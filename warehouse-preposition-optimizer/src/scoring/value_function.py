"""Movement value function: V(m) = (T_saved * P_load * W_order) / (C_move + C_opportunity)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.config import ResourceConfig
from src.models.inventory import InventoryPosition, Location
from src.models.movements import CandidateMovement
from src.models.orders import CarrierAppointment, OutboundOrder
from src.scoring.demand_predictor import DemandPredictor
from src.scoring.weights import ScoringWeights

if TYPE_CHECKING:
    from src.prediction.features import HistoricalData
    from src.prediction.inference import InferenceEngine

_ORDER_WEIGHT_MIN = 0.1
_ORDER_WEIGHT_MAX = 10.0


@dataclass
class ScoringContext:
    """Context data required to score a candidate movement.

    Args:
        orders: Outbound orders within the planning horizon.
        appointments: Carrier appointments within the planning horizon.
        resource_utilization: Current fleet utilization fraction [0.0, 1.0].
        inventory_by_sku: Optional map of sku_id → InventoryPosition for ML features.
        historical_data: Optional historical demand statistics for ML features.
    """

    orders: list[OutboundOrder] = field(default_factory=list)
    appointments: list[CarrierAppointment] = field(default_factory=list)
    resource_utilization: float = 0.0
    inventory_by_sku: dict[str, InventoryPosition] = field(default_factory=dict)
    historical_data: HistoricalData | None = None


class MovementScorer:
    """Computes value function scores for candidate movements.

    Implements V(m) = (T_saved * P_load * W_order) / (C_move + C_opportunity)
    where each term is weighted by the ScoringWeights configuration.

    Phase 1: P_load uses binary order lookup.
    Phase 2: When ml_inference is provided, P_load uses LightGBM + circuit breaker
             and SHAP contributions are stored in score_components.

    Args:
        weights: Scoring weight configuration.
        config: Physical resource configuration for cost estimates.
        ml_inference: Optional Phase 2 InferenceEngine. If None, Phase 1 path is used.
    """

    def __init__(
        self,
        weights: ScoringWeights,
        config: ResourceConfig,
        ml_inference: InferenceEngine | None = None,
        dock_door_coords: dict[int, tuple[float, float]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._weights = weights
        self._config = config
        self._phase1_predictor = DemandPredictor()
        self._ml_inference = ml_inference
        self._dock_door_coords = dock_door_coords or {}
        self._clock = clock or (lambda: datetime.now(UTC))

    def score(
        self, candidate: CandidateMovement, context: ScoringContext
    ) -> float:
        """Compute V(m) for a candidate movement and store components on the candidate.

        Short-circuits to 0.0 if T_saved <= 0 (movement won't save time) or
        P_load == 0.0 (SKU won't load on any appointment in the window).

        Args:
            candidate: The candidate movement to score. score_components is populated.
            context: Scoring context with orders, appointments, and utilization.

        Returns:
            The computed score V(m), or 0.0 if the movement is not beneficial.
        """
        best_appointment: CarrierAppointment | None = None
        best_p_load = 0.0
        best_w_order = 0.0
        shap_values: dict[str, float] = {}

        inventory_position = context.inventory_by_sku.get(candidate.sku_id)

        for appointment in context.appointments:
            p_load = self._compute_load_probability(
                candidate.sku_id,
                appointment,
                context.orders,
                inventory_position=inventory_position,
                historical_data=context.historical_data,
            )
            if p_load > 0.0:
                for order in context.orders:
                    if order.appointment.appointment_id == appointment.appointment_id:
                        w_order = self._compute_order_weight(order)
                        if w_order > best_w_order or best_appointment is None:
                            best_appointment = appointment
                            best_p_load = p_load
                            best_w_order = w_order

        if best_appointment is None or best_p_load == 0.0:
            candidate.score_components = {
                "t_saved": 0.0,
                "p_load": 0.0,
                "w_order": 0.0,
                "c_move": 0.0,
                "c_opportunity": 0.0,
            }
            candidate.score = 0.0
            return 0.0

        # Gather SHAP explanation when ML is active (best-effort; no exception on failure)
        if self._ml_inference is not None:
            shap_values = self._ml_inference.explain(
                sku_id=candidate.sku_id,
                appointment=best_appointment,
                orders=context.orders,
                inventory_position=inventory_position,
                historical_data=context.historical_data,
            )

        dock_door_x, dock_door_y = _dock_door_coords(best_appointment.dock_door, self._dock_door_coords)

        t_saved = self._compute_time_saved(
            candidate.from_location,
            candidate.to_location,
            dock_door_x,
            dock_door_y,
        )

        if t_saved <= 0.0:
            candidate.score_components = {
                "t_saved": t_saved,
                "p_load": best_p_load,
                "w_order": best_w_order,
                "c_move": 0.0,
                "c_opportunity": 0.0,
                **{f"shap_{k}": v for k, v in shap_values.items()},
            }
            candidate.score = 0.0
            return 0.0

        c_move = self._compute_movement_cost(candidate.from_location, candidate.to_location)
        c_opportunity = self._compute_opportunity_cost(context.resource_utilization)

        denominator = (
            self._weights.movement_cost * c_move
            + self._weights.opportunity_cost * c_opportunity
        )
        if denominator <= 0.0:
            denominator = 1.0

        numerator = (
            self._weights.time_saved * t_saved
            * self._weights.load_probability * best_p_load
            * self._weights.order_priority * best_w_order
        )

        score = numerator / denominator

        candidate.score_components = {
            "t_saved": t_saved,
            "p_load": best_p_load,
            "w_order": best_w_order,
            "c_move": c_move,
            "c_opportunity": c_opportunity,
            "numerator": numerator,
            "denominator": denominator,
            **{f"shap_{k}": v for k, v in shap_values.items()},
        }
        candidate.score = score
        return score

    def _compute_time_saved(
        self,
        from_loc: Location,
        to_loc: Location,
        dock_door_x: float,
        dock_door_y: float,
    ) -> float:
        """Compute seconds saved by moving SKU closer to dock door.

        Uses Manhattan distance difference converted to seconds at forklift speed.

        Args:
            from_loc: Current location of the SKU.
            to_loc: Proposed staging location.
            dock_door_x: X coordinate of the target dock door.
            dock_door_y: Y coordinate of the target dock door.

        Returns:
            Seconds saved (positive means the move saves time). Can be negative.
        """
        dist_from = abs(from_loc.x - dock_door_x) + abs(from_loc.y - dock_door_y)
        dist_to = abs(to_loc.x - dock_door_x) + abs(to_loc.y - dock_door_y)
        distance_saved = dist_from - dist_to
        return distance_saved / self._config.forklift_speed_mps

    def _compute_load_probability(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
        inventory_position: InventoryPosition | None = None,
        historical_data: HistoricalData | None = None,
    ) -> float:
        """Delegate to ML inference engine (Phase 2) or binary lookup (Phase 1).

        Args:
            sku_id: SKU identifier.
            appointment: Target carrier appointment.
            orders: All outbound orders in the horizon.
            inventory_position: Current inventory position (for ML feature quality).
            historical_data: Historical demand statistics (for ML feature quality).

        Returns:
            Probability in [0.0, 1.0].
        """
        if self._ml_inference is not None:
            return self._ml_inference.predict(
                sku_id=sku_id,
                appointment=appointment,
                orders=orders,
                inventory_position=inventory_position,
                historical_data=historical_data,
            )
        return self._phase1_predictor.predict(sku_id, appointment, orders)

    def _compute_order_weight(self, order: OutboundOrder) -> float:
        """Compute urgency-weighted order priority.

        W_order = priority * exp(-time_until_cutoff / decay_constant), clamped to [0.1, 10.0].

        Args:
            order: The outbound order.

        Returns:
            Urgency weight in [0.1, 10.0].
        """
        now = self._clock()
        cutoff = order.cutoff_time
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)

        time_until_cutoff = (cutoff - now).total_seconds()
        # Preserve the original urgency formula while preventing exp overflow
        # when scoring historical cutoffs through an injected replay clock.
        exponent = -time_until_cutoff / self._weights.decay_constant_seconds
        decay = math.exp(max(-700.0, min(700.0, exponent)))
        raw_weight = order.priority * decay
        return max(_ORDER_WEIGHT_MIN, min(_ORDER_WEIGHT_MAX, raw_weight))

    def _compute_movement_cost(
        self, from_loc: Location, to_loc: Location
    ) -> float:
        """Compute total time cost of executing the movement in seconds.

        Args:
            from_loc: Origin location.
            to_loc: Destination location.

        Returns:
            Travel time + handling time in seconds.
        """
        distance = abs(from_loc.x - to_loc.x) + abs(from_loc.y - to_loc.y)
        travel_time = distance / self._config.forklift_speed_mps
        return travel_time + self._config.handling_time_seconds

    def _compute_opportunity_cost(self, resource_utilization: float) -> float:
        """Compute opportunity cost based on fleet utilization.

        Formula: base * (1 / (1 - min(util, 0.95)))
        High utilization makes each resource more expensive to commit.

        Args:
            resource_utilization: Current fleet utilization [0.0, 1.0].

        Returns:
            Opportunity cost in seconds.
        """
        capped_util = min(resource_utilization, 0.95)
        return self._config.base_opportunity_seconds * (1.0 / (1.0 - capped_util))


# Default dock door coordinates used when no explicit map is provided.
# Override via MovementScorer(dock_door_coords={1: (0.0, 5.0), 2: (0.0, 10.0), ...}).
_DEFAULT_DOCK_DOOR_COORDS: dict[int, tuple[float, float]] = {}


def _dock_door_coords(
    dock_door: int,
    coord_map: dict[int, tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """Return (x, y) coordinates for a dock door in meters.

    Resolution order:
    1. ``coord_map`` argument (passed from MovementScorer config).
    2. Module-level ``_DEFAULT_DOCK_DOOR_COORDS`` (set at app startup from config.yml).
    3. Placeholder: x=0, y=door_number * 5 — clearly approximate, logs a warning once.

    Args:
        dock_door: Dock door number.
        coord_map: Optional explicit mapping of door number to (x, y).

    Returns:
        (x, y) coordinate tuple in meters.
    """
    if coord_map and dock_door in coord_map:
        return coord_map[dock_door]
    if dock_door in _DEFAULT_DOCK_DOOR_COORDS:
        return _DEFAULT_DOCK_DOOR_COORDS[dock_door]
    # Placeholder — replace with real coordinates (see HUMAN_TODO.md item 3).
    return 0.0, float(dock_door) * 5.0
