"""Feature engineering pipeline for ML demand prediction (Phase 2)."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.models.inventory import ABCClass, InventoryPosition
from src.models.orders import CarrierAppointment, OutboundOrder

# Canonical ordered feature list — training and inference must use this order.
FEATURE_NAMES: list[str] = [
    # Temporal
    "hour_of_day_sin",
    "hour_of_day_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    "days_until_month_end",
    "is_holiday",
    # SKU-level
    "abc_class_ordinal",
    "avg_daily_demand_30d",
    "demand_cv_30d",
    "days_since_last_shipment",
    "current_on_hand_quantity",
    "pending_order_quantity",
    # Dock-level
    "carrier_id_encoded",
    "carrier_sku_frequency",
    "appointment_duration_minutes",
    "dock_zone_match",
    # Order pipeline
    "order_exists_for_sku",
    "order_priority",
    "minutes_until_cutoff",
    "order_fill_rate",
]

_ABC_ORDINAL: dict[ABCClass, float] = {
    ABCClass.A: 3.0,
    ABCClass.B: 2.0,
    ABCClass.C: 1.0,
}


@dataclass
class HistoricalData:
    """Historical WMS signals used as ML features.

    All dicts default to empty; missing keys are imputed with 0 or domain
    defaults so callers never need to pre-fill every SKU.

    Args:
        avg_daily_demand: sku_id → average units shipped per day (30-day window).
        demand_cv: sku_id → coefficient of variation of daily demand.
        days_since_last_shipment: sku_id → days elapsed since last outbound movement.
        carrier_sku_frequency: (carrier, sku_id) → fraction of carrier appointments
            that included this SKU (0.0–1.0).
        carrier_id_encoding: carrier name → integer label (for model input).
    """

    avg_daily_demand: dict[str, float] = field(default_factory=dict)
    demand_cv: dict[str, float] = field(default_factory=dict)
    days_since_last_shipment: dict[str, float] = field(default_factory=dict)
    carrier_sku_frequency: dict[tuple[str, str], float] = field(default_factory=dict)
    carrier_id_encoding: dict[str, int] = field(default_factory=dict)


class FeatureBuilder:
    """Builds flat float feature vectors for ML demand prediction.

    All output values are floats with no nulls. Missing historical signals
    are imputed with 0 or domain-appropriate defaults.

    Public interface:
        build_features(sku_id, appointment, orders, inventory_position, historical_data, now)
    """

    def build_features(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
        inventory_position: InventoryPosition | None = None,
        historical_data: HistoricalData | None = None,
        now: datetime | None = None,
    ) -> dict[str, float]:
        """Build feature dict for predicting P(SKU_i loads at appointment).

        Args:
            sku_id: SKU identifier to compute features for.
            appointment: Carrier appointment being evaluated.
            orders: All outbound orders in the planning horizon.
            inventory_position: Current inventory position for this SKU, if known.
            historical_data: Historical demand and carrier statistics; imputed if None.
            now: Reference timestamp; defaults to UTC now.

        Returns:
            Dict mapping each FEATURE_NAMES entry to a float. Always has exactly
            len(FEATURE_NAMES) keys, no nulls.
        """
        hist = historical_data or HistoricalData()
        ref = now or datetime.now(UTC)

        temporal = self._temporal_features(ref)
        sku_feats = self._sku_features(sku_id, inventory_position, hist)
        order_feats = self._order_pipeline_features(sku_id, appointment, orders, ref)
        dock_feats = self._dock_features(sku_id, appointment, inventory_position, hist)

        return {**temporal, **sku_feats, **dock_feats, **order_feats}

    # ──────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _temporal_features(self, now: datetime) -> dict[str, float]:
        """Cyclically-encoded temporal features.

        Args:
            now: Reference timestamp.

        Returns:
            Six temporal feature values.
        """
        hour = now.hour + now.minute / 60.0
        dow = float(now.weekday())  # 0 = Monday

        days_in_month = calendar.monthrange(now.year, now.month)[1]
        days_until_eom = float(days_in_month - now.day)

        return {
            "hour_of_day_sin": math.sin(2 * math.pi * hour / 24.0),
            "hour_of_day_cos": math.cos(2 * math.pi * hour / 24.0),
            "day_of_week_sin": math.sin(2 * math.pi * dow / 7.0),
            "day_of_week_cos": math.cos(2 * math.pi * dow / 7.0),
            "days_until_month_end": days_until_eom,
            # Placeholder: integrate holiday calendar in a future iteration.
            "is_holiday": 0.0,
        }

    def _sku_features(
        self,
        sku_id: str,
        position: InventoryPosition | None,
        hist: HistoricalData,
    ) -> dict[str, float]:
        """SKU velocity and inventory features.

        Args:
            sku_id: SKU identifier.
            position: Inventory position for on-hand quantity and ABC class.
            hist: Historical demand statistics.

        Returns:
            Six SKU-level feature values.
        """
        abc = position.sku.abc_class if position else ABCClass.C
        on_hand = float(position.quantity) if position else 0.0

        return {
            "abc_class_ordinal": _ABC_ORDINAL[abc],
            "avg_daily_demand_30d": hist.avg_daily_demand.get(sku_id, 0.0),
            "demand_cv_30d": hist.demand_cv.get(sku_id, 0.0),
            # Default 30 days if unknown — implies SKU hasn't shipped recently.
            "days_since_last_shipment": hist.days_since_last_shipment.get(sku_id, 30.0),
            "current_on_hand_quantity": on_hand,
            "pending_order_quantity": 0.0,  # overwritten by order pipeline below
        }

    def _order_pipeline_features(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
        now: datetime,
    ) -> dict[str, float]:
        """Order pipeline signals for the given SKU/appointment pair.

        Args:
            sku_id: SKU identifier.
            appointment: Target appointment.
            orders: All horizon orders.
            now: Reference timestamp.

        Returns:
            Five order pipeline feature values.
        """
        order_exists = 0.0
        max_priority = 0.0
        min_cutoff_minutes = 0.0
        pending_qty = 0.0
        total_lines = 0
        picked_lines = 0

        for order in orders:
            if order.appointment.appointment_id != appointment.appointment_id:
                continue
            for line in order.lines:
                total_lines += 1
                if line.picked:
                    picked_lines += 1
                if line.sku_id == sku_id:
                    order_exists = 1.0
                    if not line.picked:
                        pending_qty += float(line.quantity)
                    max_priority = max(max_priority, float(order.priority))
                    cutoff = order.cutoff_time
                    if cutoff.tzinfo is None:
                        cutoff = cutoff.replace(tzinfo=UTC)
                    mins = (cutoff - now).total_seconds() / 60.0
                    if min_cutoff_minutes == 0.0 or mins < min_cutoff_minutes:
                        min_cutoff_minutes = mins

        fill_rate = float(picked_lines) / float(total_lines) if total_lines > 0 else 0.0

        return {
            "pending_order_quantity": pending_qty,
            "order_exists_for_sku": order_exists,
            "order_priority": max_priority,
            "minutes_until_cutoff": min_cutoff_minutes,
            "order_fill_rate": fill_rate,
        }

    def _dock_features(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        position: InventoryPosition | None,
        hist: HistoricalData,
    ) -> dict[str, float]:
        """Dock-level and carrier features.

        Args:
            sku_id: SKU identifier.
            appointment: Target appointment.
            position: Inventory position (used for dock-zone proximity).
            hist: Historical carrier statistics.

        Returns:
            Four dock-level feature values.
        """
        carrier = appointment.carrier
        carrier_enc = float(hist.carrier_id_encoding.get(carrier, 0))
        carrier_freq = hist.carrier_sku_frequency.get((carrier, sku_id), 0.0)

        duration_mins = (
            appointment.scheduled_departure - appointment.scheduled_arrival
        ).total_seconds() / 60.0

        dock_zone_match = 0.0
        if (
            position is not None
            and position.location.nearest_dock_door == appointment.dock_door
        ):
            dock_zone_match = 1.0

        return {
            "carrier_id_encoded": carrier_enc,
            "carrier_sku_frequency": carrier_freq,
            "appointment_duration_minutes": duration_mins,
            "dock_zone_match": dock_zone_match,
        }
