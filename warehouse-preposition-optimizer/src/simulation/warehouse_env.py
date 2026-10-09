"""Gymnasium environment wrapping the SimPy warehouse digital twin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, SupportsFloat

import numpy as np
import numpy.typing as npt
import gymnasium as gym
from gymnasium import spaces

from src.models.inventory import (
    ABCClass,
    InventoryPosition,
    Location,
    SKU,
    TemperatureZone,
)
from src.models.movements import CandidateMovement
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.simulation.digital_twin import SimConfig, SimMovement, WarehouseDigitalTwin
from src.simulation.reward import RewardWeights, compute_shaping_reward, compute_step_reward


# Index reserved for the NO_OP action (agent chooses not to move any SKU).
_NO_OP_INDEX = 0

# Placeholder feature dimensions — scaled to match realistic warehouse sizes.
_MAX_CANDIDATES = 20
_MAX_ORDERS = 10
_MAX_DOCKS = 4
_SCHEDULE_FEATURES = 3  # hours_until_arrival, hours_until_departure, dock_door_normalised
_ORDER_FEATURES = 4  # priority, minutes_until_cutoff, sku_count, fill_rate
_TIME_FEATURES = 2  # hour_sin, hour_cos
_RESOURCE_FEATURES = 1  # fleet_utilization


@dataclass
class EnvConfig:
    """Configuration for WarehousePrePositionEnv.

    Args:
        sim_config: SimPy simulation parameters.
        reward_weights: Reward shaping weights.
        max_candidates: Maximum repositioning candidates per step.
        num_locations: Total number of warehouse locations (for obs encoding).
        num_docks: Number of dock doors.
        max_orders: Maximum orders per observation.
        seed: RNG seed for episode reproducibility.
    """

    sim_config: SimConfig = field(default_factory=SimConfig)
    reward_weights: RewardWeights = field(default_factory=RewardWeights)
    max_candidates: int = _MAX_CANDIDATES
    num_locations: int = 100
    num_docks: int = _MAX_DOCKS
    max_orders: int = _MAX_ORDERS
    seed: int = 42


class WarehousePrePositionEnv(gym.Env):
    """Gymnasium environment for warehouse pre-positioning via RL.

    **State space (Box, float32):**
    - Candidate features: (max_candidates, 6) — score, t_saved, p_load, w_order, c_move, c_opp
    - Order queue: (max_orders, ORDER_FEATURES)
    - Dock schedule: (num_docks, SCHEDULE_FEATURES)
    - Global: [fleet_utilization, hour_sin, hour_cos]

    The full observation is the concatenation of all the above flattened to 1-D.

    **Action space (Discrete):**
    - 0: NO_OP — do not dispatch any movement this step
    - 1..max_candidates: dispatch candidate at index (action - 1)

    **Action masking:**
    - ``action_masks()`` returns a bool array; masked actions (infeasible or no
      candidate at that index) are set to False.

    **Episode:**
    - One 8-hour shift per episode.
    - Each step corresponds to one dispatch decision.
    - Episode terminates when shift_duration_seconds elapses in the digital twin.

    Args:
        env_config: Environment configuration.
        candidates_fn: Callable that returns the current list of CandidateMovement
            for the current env state. Injected so the env is testable without
            running the full scheduler.
        inventory: Initial inventory positions for the shift.
        appointments: Carrier appointments for the shift.
        orders: Outbound orders for the shift.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        env_config: EnvConfig | None = None,
        candidates_fn: Any = None,
        inventory: list[InventoryPosition] | None = None,
        appointments: list[CarrierAppointment] | None = None,
        orders: list[OutboundOrder] | None = None,
    ) -> None:
        super().__init__()
        self._cfg = env_config or EnvConfig()
        self._candidates_fn = candidates_fn  # () -> list[CandidateMovement]
        self._inventory = inventory or []
        self._appointments = appointments or []
        self._orders = orders or []

        # Observation: all feature blocks concatenated and flattened.
        candidate_size = self._cfg.max_candidates * 6
        order_size = self._cfg.max_orders * _ORDER_FEATURES
        dock_size = self._cfg.num_docks * _SCHEDULE_FEATURES
        global_size = _RESOURCE_FEATURES + _TIME_FEATURES
        obs_size = candidate_size + order_size + dock_size + global_size

        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(obs_size,),
            dtype=np.float32,
        )

        # Actions: 0 = NO_OP, 1..max_candidates = pick that candidate.
        self.action_space = spaces.Discrete(self._cfg.max_candidates + 1)

        # Internal state
        self._candidates: list[CandidateMovement] = []
        self._sim_time: float = 0.0
        self._step_count: int = 0
        self._twin: WarehouseDigitalTwin | None = None
        self._total_reward: float = 0.0

    # ------------------------------------------------------------------
    # Gymnasium interface
    # ------------------------------------------------------------------

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[npt.NDArray[np.float32], dict[str, Any]]:
        """Reset the environment to the start of a new shift.

        Args:
            seed: Optional RNG seed override.
            options: Unused; reserved for future use.

        Returns:
            Initial observation and empty info dict.
        """
        super().reset(seed=seed)

        effective_seed = seed if seed is not None else self._cfg.seed
        sim_cfg = SimConfig(
            shift_duration_seconds=self._cfg.sim_config.shift_duration_seconds,
            forklift_count=self._cfg.sim_config.forklift_count,
            forklift_speed_mps=self._cfg.sim_config.forklift_speed_mps,
            handling_time_seconds=self._cfg.sim_config.handling_time_seconds,
            loading_time_per_pallet_seconds=self._cfg.sim_config.loading_time_per_pallet_seconds,
            staging_loading_speedup=self._cfg.sim_config.staging_loading_speedup,
            random_seed=effective_seed,
        )

        self._twin = WarehouseDigitalTwin(
            config=sim_cfg,
            inventory=self._inventory,
            appointments=self._appointments,
            orders=self._orders,
        )
        self._sim_time = 0.0
        self._step_count = 0
        self._total_reward = 0.0
        self._candidates = self._get_candidates()

        obs = self._build_observation()
        return obs, {}

    def step(
        self, action: int
    ) -> tuple[npt.NDArray[np.float32], float, bool, bool, dict[str, Any]]:
        """Execute one dispatch decision.

        Args:
            action: Index into action space.
                0 → NO_OP, 1..N → dispatch candidates[action-1].

        Returns:
            Tuple of (observation, reward, terminated, truncated, info).
        """
        assert self._twin is not None, "Call reset() before step()"

        reward = 0.0
        avg_dist_before = self._avg_dist_to_nearest_dock()

        if action != _NO_OP_INDEX:
            candidate_idx = action - 1
            if 0 <= candidate_idx < len(self._candidates):
                cand = self._candidates[candidate_idx]
                dist = (
                    abs(cand.from_location.x - cand.to_location.x)
                    + abs(cand.from_location.y - cand.to_location.y)
                )
                movement_cost = dist / self._cfg.sim_config.forklift_speed_mps
                t_saved = cand.score_components.get("t_saved", 0.0)

                # Apply move to twin's inventory state immediately.
                sim_move = SimMovement(
                    sku_id=cand.sku_id,
                    from_location=cand.from_location,
                    to_location=cand.to_location,
                    distance_meters=dist,
                    score=cand.score,
                )
                self._twin.apply_movement(sim_move)
                self._twin.metrics.movements_executed += 1
                self._twin.metrics.total_movement_cost_seconds += movement_cost

                reward += compute_step_reward(t_saved, movement_cost, self._cfg.reward_weights)

        avg_dist_after = self._avg_dist_to_nearest_dock()
        reward += compute_shaping_reward(avg_dist_before, avg_dist_after, self._cfg.reward_weights)

        # Advance sim time by one cycle interval (simulated 60s decision cadence).
        self._sim_time += 60.0
        self._step_count += 1
        self._total_reward += reward

        terminated = self._sim_time >= self._cfg.sim_config.shift_duration_seconds
        self._candidates = [] if terminated else self._get_candidates()

        obs = self._build_observation()
        info: dict[str, Any] = {
            "step": self._step_count,
            "sim_time": self._sim_time,
            "total_reward": self._total_reward,
        }
        return obs, float(reward), terminated, False, info

    def action_masks(self) -> npt.NDArray[np.bool_]:
        """Return boolean mask over action space.

        NO_OP (index 0) is always valid. Candidate slots beyond the current
        candidate count are masked out.

        Returns:
            Boolean array of shape (max_candidates + 1,).
        """
        mask = np.zeros(self._cfg.max_candidates + 1, dtype=bool)
        mask[0] = True  # NO_OP always allowed
        for i in range(len(self._candidates)):
            mask[i + 1] = True
        return mask

    def render(self) -> None:
        """Rendering not implemented (no visual output)."""
        return None

    # ------------------------------------------------------------------
    # Observation construction
    # ------------------------------------------------------------------

    def _build_observation(self) -> npt.NDArray[np.float32]:
        """Construct the flat observation vector.

        Returns:
            Float32 array of shape (obs_size,).
        """
        parts: list[npt.NDArray[np.float32]] = [
            self._encode_candidates(),
            self._encode_orders(),
            self._encode_docks(),
            self._encode_globals(),
        ]
        return np.concatenate(parts).astype(np.float32)

    def _encode_candidates(self) -> npt.NDArray[np.float32]:
        """Encode current candidates into (max_candidates, 6) flattened array.

        Features per candidate: score, t_saved, p_load, w_order, c_move, c_opp.
        """
        arr = np.zeros((self._cfg.max_candidates, 6), dtype=np.float32)
        for i, cand in enumerate(self._candidates[: self._cfg.max_candidates]):
            sc = cand.score_components
            arr[i] = [
                cand.score,
                sc.get("t_saved", 0.0),
                sc.get("p_load", 0.0),
                sc.get("w_order", 0.0),
                sc.get("c_move", 0.0),
                sc.get("c_opportunity", 0.0),
            ]
        return arr.flatten()

    def _encode_orders(self) -> npt.NDArray[np.float32]:
        """Encode outbound orders into (max_orders, ORDER_FEATURES) flattened array.

        Features: priority, minutes_until_cutoff, sku_count, fill_rate.
        """
        arr = np.zeros((self._cfg.max_orders, _ORDER_FEATURES), dtype=np.float32)
        now = datetime.now(UTC)
        for i, order in enumerate(self._orders[: self._cfg.max_orders]):
            cutoff = order.cutoff_time
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=UTC)
            minutes = max(0.0, (cutoff - now).total_seconds() / 60.0)
            picked = sum(1 for line in order.lines if line.picked)
            fill = picked / max(1, len(order.lines))
            arr[i] = [order.priority / 10.0, minutes / 480.0, len(order.lines) / 10.0, fill]
        return arr.flatten()

    def _encode_docks(self) -> npt.NDArray[np.float32]:
        """Encode dock schedule into (num_docks, SCHEDULE_FEATURES) flattened array.

        Features: hours_until_arrival, hours_until_departure, dock_door_normalised.
        """
        arr = np.zeros((self._cfg.num_docks, _SCHEDULE_FEATURES), dtype=np.float32)
        now = datetime.now(UTC)
        for i, appt in enumerate(self._appointments[: self._cfg.num_docks]):
            arrival = appt.scheduled_arrival
            departure = appt.scheduled_departure
            if arrival.tzinfo is None:
                arrival = arrival.replace(tzinfo=UTC)
            if departure.tzinfo is None:
                departure = departure.replace(tzinfo=UTC)
            h_arr = max(0.0, (arrival - now).total_seconds() / 3600.0)
            h_dep = max(0.0, (departure - now).total_seconds() / 3600.0)
            arr[i] = [h_arr / 8.0, h_dep / 8.0, appt.dock_door / max(1, self._cfg.num_docks)]
        return arr.flatten()

    def _encode_globals(self) -> npt.NDArray[np.float32]:
        """Encode global time and resource features into a short array."""
        hour = (self._sim_time % 86400) / 3600.0
        hour_sin = float(np.sin(2 * np.pi * hour / 24.0))
        hour_cos = float(np.cos(2 * np.pi * hour / 24.0))
        # Resource utilization from the twin's metrics
        util = 0.0
        if self._twin is not None and self._cfg.sim_config.shift_duration_seconds > 0:
            util = min(
                1.0,
                self._twin.metrics.total_movement_cost_seconds
                / (self._cfg.sim_config.shift_duration_seconds * self._cfg.sim_config.forklift_count),
            )
        return np.array([util, hour_sin, hour_cos], dtype=np.float32)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_candidates(self) -> list[CandidateMovement]:
        """Retrieve current candidates from the injected function, or return empty list."""
        if self._candidates_fn is not None:
            try:
                return self._candidates_fn()
            except Exception:
                pass
        return []

    def _avg_dist_to_nearest_dock(self) -> float:
        """Compute average distance from ordered SKUs to their nearest dock door.

        Used for potential-based shaping reward.

        Returns:
            Mean distance in metres.
        """
        if self._twin is None:
            return 0.0
        if not self._appointments:
            return 0.0
        dock_door = self._appointments[0].dock_door
        return self._twin.get_avg_distance_to_dock(dock_door)
