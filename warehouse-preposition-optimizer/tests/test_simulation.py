"""Tests for Phase 4 simulation components."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from src.models.inventory import ABCClass, InventoryPosition, Location, SKU, TemperatureZone
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.simulation.digital_twin import SimConfig, SimMovement, WarehouseDigitalTwin
from src.simulation.reward import (
    EpisodeMetrics,
    RewardWeights,
    compute_episode_return,
    compute_shaping_reward,
    compute_step_reward,
    compute_truck_departure_reward,
)
from src.simulation.warehouse_env import EnvConfig, WarehousePrePositionEnv


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _loc(
    location_id: str,
    x: float = 0.0,
    y: float = 0.0,
    is_staging: bool = False,
    dock_door: int = 1,
    zone: TemperatureZone = TemperatureZone.AMBIENT,
) -> Location:
    return Location(
        location_id=location_id,
        zone="BULK",
        aisle=1,
        bay=1,
        level=0,
        x=x,
        y=y,
        temperature_zone=zone,
        is_staging=is_staging,
        nearest_dock_door=dock_door,
    )


def _sku(sku_id: str = "SKU-1") -> SKU:
    return SKU(sku_id=sku_id, description="Test", weight_kg=10.0, volume_m3=0.1,
               abc_class=ABCClass.A)


def _position(sku_id: str, loc: Location, quantity: int = 5) -> InventoryPosition:
    return InventoryPosition(
        position_id=f"POS-{sku_id}",
        sku=_sku(sku_id),
        location=loc,
        quantity=quantity,
    )


def _appointment(hours_from_now: float = 1.0, dock_door: int = 1) -> CarrierAppointment:
    now = datetime.now(UTC)
    return CarrierAppointment(
        appointment_id="APPT-1",
        carrier="FedEx",
        dock_door=dock_door,
        scheduled_arrival=now + timedelta(hours=hours_from_now),
        scheduled_departure=now + timedelta(hours=hours_from_now + 2),
        status=AppointmentStatus.SCHEDULED,
    )


def _order(appt: CarrierAppointment, sku_ids: list[str]) -> OutboundOrder:
    now = datetime.now(UTC)
    return OutboundOrder(
        order_id="ORD-1",
        appointment=appt,
        lines=[OrderLine(line_id=f"L{i}", sku_id=sid, quantity=1) for i, sid in enumerate(sku_ids)],
        priority=5,
        cutoff_time=now + timedelta(hours=3),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Reward functions
# ──────────────────────────────────────────────────────────────────────────────


def test_step_reward_positive_when_time_saved() -> None:
    w = RewardWeights(r1_seconds_saved=1.0, r2_movement_cost=0.05)
    r = compute_step_reward(seconds_saved=60.0, movement_cost_seconds=10.0, weights=w)
    assert r > 0.0


def test_step_reward_negative_when_no_savings() -> None:
    w = RewardWeights(r1_seconds_saved=1.0, r2_movement_cost=1.0)
    r = compute_step_reward(seconds_saved=0.0, movement_cost_seconds=20.0, weights=w)
    assert r < 0.0


def test_truck_departure_reward_early() -> None:
    w = RewardWeights(r3_early_departure=0.5)
    r = compute_truck_departure_reward(
        actual_departure_seconds=100.0,
        scheduled_departure_seconds=200.0,
        weights=w,
    )
    assert r > 0.0
    assert abs(r - 0.5 * 100.0) < 1e-6


def test_truck_departure_reward_late() -> None:
    w = RewardWeights(r4_late_departure=2.0)
    r = compute_truck_departure_reward(
        actual_departure_seconds=300.0,
        scheduled_departure_seconds=200.0,
        weights=w,
    )
    assert r < 0.0


def test_shaping_reward_positive_when_distance_decreases() -> None:
    w = RewardWeights(r5_distance_shaping=0.01)
    r = compute_shaping_reward(avg_distance_before=50.0, avg_distance_after=30.0, weights=w)
    assert r > 0.0


def test_shaping_reward_negative_when_distance_increases() -> None:
    w = RewardWeights(r5_distance_shaping=0.01)
    r = compute_shaping_reward(avg_distance_before=10.0, avg_distance_after=20.0, weights=w)
    assert r < 0.0


def test_episode_return_computation() -> None:
    w = RewardWeights(r1_seconds_saved=1.0, r2_movement_cost=0.1,
                      r3_early_departure=0.5, r4_late_departure=2.0)
    m = EpisodeMetrics(
        total_seconds_saved=120.0,
        total_movement_cost_seconds=30.0,
        early_departure_seconds=60.0,
        late_departure_seconds=0.0,
    )
    ret = compute_episode_return(m, w)
    expected = 1.0 * 120.0 - 0.1 * 30.0 + 0.5 * 60.0
    assert abs(ret - expected) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# WarehouseDigitalTwin
# ──────────────────────────────────────────────────────────────────────────────


def test_digital_twin_run_returns_metrics() -> None:
    """Digital twin should run and return an EpisodeMetrics instance."""
    cfg = SimConfig(shift_duration_seconds=3600.0, forklift_count=2, random_seed=0)
    appt = _appointment(hours_from_now=0.1)
    inv = [_position("SKU-1", _loc("L1", x=10.0))]
    orders = [_order(appt, ["SKU-1"])]

    twin = WarehouseDigitalTwin(config=cfg, inventory=inv, appointments=[appt], orders=orders)
    metrics = twin.run()

    assert isinstance(metrics, EpisodeMetrics)
    assert metrics.trucks_served >= 0


def test_digital_twin_staged_sku_improves_seconds_saved() -> None:
    """Pre-staging a SKU near the dock should register seconds saved vs bulk storage."""
    cfg = SimConfig(
        shift_duration_seconds=3600.0,
        forklift_count=2,
        loading_time_per_pallet_seconds=60.0,
        staging_loading_speedup=0.5,
        random_seed=0,
    )
    appt = _appointment(hours_from_now=0.1, dock_door=1)
    orders = [_order(appt, ["SKU-A"])]

    # Staged version
    staged_loc = _loc("STAGE-1", x=1.0, is_staging=True, dock_door=1)
    staged_inv = [_position("SKU-A", staged_loc)]
    twin_staged = WarehouseDigitalTwin(config=cfg, inventory=staged_inv, appointments=[appt], orders=orders)
    metrics_staged = twin_staged.run()

    # Bulk version
    bulk_loc = _loc("BULK-1", x=50.0, is_staging=False)
    bulk_inv = [_position("SKU-A", bulk_loc)]
    twin_bulk = WarehouseDigitalTwin(config=cfg, inventory=bulk_inv, appointments=[appt], orders=orders)
    metrics_bulk = twin_bulk.run()

    assert metrics_staged.total_seconds_saved > metrics_bulk.total_seconds_saved


def test_digital_twin_movement_execution() -> None:
    """Executing a pre-positioning movement should update the inventory location."""
    cfg = SimConfig(shift_duration_seconds=60.0, forklift_count=1, random_seed=0)
    from_loc = _loc("FROM", x=20.0)
    to_loc = _loc("STAGE", x=2.0, is_staging=True, dock_door=1)
    inv = [_position("SKU-1", from_loc)]
    movement = SimMovement(
        sku_id="SKU-1",
        from_location=from_loc,
        to_location=to_loc,
        distance_meters=18.0,
        score=1.5,
    )
    twin = WarehouseDigitalTwin(
        config=cfg, inventory=inv, appointments=[], orders=[], pending_movements=[movement]
    )
    twin.run()

    assert twin.metrics.movements_executed == 1
    # After movement, SKU should be at the staging location
    assert twin._inventory["SKU-1"].location.location_id == "STAGE"


def test_digital_twin_avg_distance_to_dock() -> None:
    appt = _appointment(hours_from_now=1.0, dock_door=2)
    loc = _loc("L1", x=10.0, y=0.0)
    inv = [_position("SKU-1", loc)]
    orders = [_order(appt, ["SKU-1"])]
    cfg = SimConfig(shift_duration_seconds=3600.0, forklift_count=1, random_seed=0)
    twin = WarehouseDigitalTwin(config=cfg, inventory=inv, appointments=[appt], orders=orders)

    dist = twin.get_avg_distance_to_dock(dock_door=2)
    assert dist > 0.0


# ──────────────────────────────────────────────────────────────────────────────
# WarehousePrePositionEnv
# ──────────────────────────────────────────────────────────────────────────────


def _make_env(candidates: list | None = None) -> WarehousePrePositionEnv:
    appt = _appointment(hours_from_now=1.0)
    inv = [_position("SKU-1", _loc("L1", x=10.0))]
    orders = [_order(appt, ["SKU-1"])]
    cfg = EnvConfig(
        sim_config=SimConfig(shift_duration_seconds=300.0, forklift_count=1, random_seed=0),
        seed=0,
    )
    candidates_fn = (lambda: candidates) if candidates is not None else None
    return WarehousePrePositionEnv(
        env_config=cfg,
        candidates_fn=candidates_fn,
        inventory=inv,
        appointments=[appt],
        orders=orders,
    )


def test_env_reset_returns_obs_and_info() -> None:
    env = _make_env()
    obs, info = env.reset()
    assert obs.dtype == np.float32
    assert obs.ndim == 1
    assert obs.shape == env.observation_space.shape
    assert isinstance(info, dict)


def test_env_observation_space_shape() -> None:
    env = _make_env()
    obs, _ = env.reset()
    assert env.observation_space.contains(obs)


def test_env_action_space_is_discrete() -> None:
    from gymnasium import spaces
    env = _make_env()
    assert isinstance(env.action_space, spaces.Discrete)


def test_env_step_noop_returns_valid_tuple() -> None:
    env = _make_env()
    env.reset()
    obs, reward, terminated, truncated, info = env.step(0)  # NO_OP
    assert obs.shape == env.observation_space.shape
    assert isinstance(reward, float)
    assert isinstance(terminated, bool)
    assert isinstance(truncated, bool)
    assert "step" in info


def test_env_terminates_after_shift_duration() -> None:
    """Environment should terminate once sim_time >= shift_duration_seconds."""
    env = _make_env()
    env.reset()
    terminated = False
    max_steps = 1000
    for _ in range(max_steps):
        _, _, terminated, _, _ = env.step(0)
        if terminated:
            break
    assert terminated, "Environment should have terminated within max_steps"


def test_env_action_masks_no_op_always_valid() -> None:
    env = _make_env()
    env.reset()
    mask = env.action_masks()
    assert mask[0] is True or bool(mask[0])  # NO_OP always on


def test_env_action_masks_shape() -> None:
    env = _make_env()
    env.reset()
    mask = env.action_masks()
    assert mask.shape == (env.action_space.n,)


def test_env_step_with_candidate_action() -> None:
    """Providing a candidate action (index 1) should not raise."""
    from src.models.movements import CandidateMovement

    from_loc = _loc("FROM", x=10.0)
    to_loc = _loc("STAGE", x=2.0, is_staging=True, dock_door=1)
    cand = CandidateMovement(
        sku_id="SKU-1",
        from_location=from_loc,
        to_location=to_loc,
        score=2.5,
        score_components={"t_saved": 30.0, "p_load": 1.0, "w_order": 1.0,
                          "c_move": 10.0, "c_opportunity": 5.0},
        reason="test",
    )
    env = _make_env(candidates=[cand])
    env.reset()
    obs, reward, terminated, truncated, info = env.step(1)
    assert obs.shape == env.observation_space.shape
    assert isinstance(reward, float)
