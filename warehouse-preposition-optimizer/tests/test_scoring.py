"""Tests for the movement scoring value function."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import ResourceConfig
from src.ingestion.wms_adapter import WarehouseState
from src.models.inventory import ABCClass, InventoryPosition, Location, SKU, TemperatureZone
from src.models.movements import CandidateMovement
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.scoring.value_function import MovementScorer, ScoringContext
from src.scoring.weights import ScoringWeights


def _default_resource_config() -> ResourceConfig:
    """Return a standard ResourceConfig for tests."""
    return ResourceConfig(
        forklift_speed_mps=2.2,
        agv_speed_mps=1.3,
        handling_time_seconds=45.0,
        max_utilization=0.95,
        base_opportunity_seconds=60.0,
    )


def _make_location(
    location_id: str,
    x: float,
    y: float,
    is_staging: bool = False,
    nearest_dock_door: int | None = None,
) -> Location:
    """Build a simple Location for testing."""
    return Location(
        location_id=location_id,
        zone="A",
        aisle=1,
        bay=1,
        level=0,
        x=x,
        y=y,
        temperature_zone=TemperatureZone.AMBIENT,
        is_staging=is_staging,
        nearest_dock_door=nearest_dock_door,
    )


def _make_sku(sku_id: str = "SKU-001") -> SKU:
    """Build a basic ambient SKU for testing."""
    return SKU(
        sku_id=sku_id,
        description="Test SKU",
        weight_kg=50.0,
        volume_m3=0.3,
        abc_class=ABCClass.A,
    )


def _make_appointment(
    dock_door: int = 1, hours_from_now: float = 2.0
) -> CarrierAppointment:
    """Build a test carrier appointment."""
    now = datetime.now(UTC)
    return CarrierAppointment(
        appointment_id=f"APPT-{dock_door}",
        carrier="Test Carrier",
        dock_door=dock_door,
        scheduled_arrival=now + timedelta(hours=hours_from_now),
        scheduled_departure=now + timedelta(hours=hours_from_now + 1),
        status=AppointmentStatus.SCHEDULED,
    )


def _make_order(
    appointment: CarrierAppointment,
    sku_id: str,
    priority: int = 5,
    cutoff_hours: float = 2.5,
) -> OutboundOrder:
    """Build a test outbound order."""
    now = datetime.now(UTC)
    return OutboundOrder(
        order_id="ORD-001",
        appointment=appointment,
        lines=[OrderLine(line_id="L1", sku_id=sku_id, quantity=10)],
        priority=priority,
        cutoff_time=now + timedelta(hours=cutoff_hours),
    )


def _make_state_with_positions(
    sku: SKU, location: Location
) -> WarehouseState:
    """Build a minimal WarehouseState with one inventory position."""
    return WarehouseState(
        inventory_positions=[
            InventoryPosition(
                position_id="POS-001",
                sku=sku,
                location=location,
                quantity=5,
            )
        ],
        outbound_orders=[],
        appointments=[],
        staging_locations=[],
        resource_utilization={},
        location_utilization={},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Core value function tests
# ──────────────────────────────────────────────────────────────────────────────

def test_zero_distance_savings_returns_zero_score() -> None:
    """If to_location is the same distance from dock as from_location, score is 0."""
    sku = _make_sku()
    # Dock door 1 is at (0.0, 5.0) per _dock_door_coords
    from_loc = _make_location("FROM", x=10.0, y=5.0)
    to_loc = _make_location("TO", x=10.0, y=5.0)  # Same position as from

    appointment = _make_appointment(dock_door=1)
    order = _make_order(appointment, sku.sku_id)
    state = _make_state_with_positions(sku, from_loc)
    context = ScoringContext(
        orders=[order], appointments=[appointment], resource_utilization=0.2
    )
    candidate = CandidateMovement(sku_id=sku.sku_id, from_location=from_loc, to_location=to_loc)

    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())
    score = scorer.score(candidate, context)
    assert score == 0.0


def test_no_matching_order_returns_zero_score() -> None:
    """If SKU does not appear on any order, P_load=0 → score=0."""
    sku = _make_sku("SKU-NOT-ORDERED")
    from_loc = _make_location("FAR", x=100.0, y=50.0)
    to_loc = _make_location("CLOSE", x=2.0, y=5.0, is_staging=True, nearest_dock_door=1)

    appointment = _make_appointment(dock_door=1)
    # Order for a different SKU
    order = _make_order(appointment, "SKU-DIFFERENT")
    state = _make_state_with_positions(sku, from_loc)
    context = ScoringContext(
        orders=[order], appointments=[appointment], resource_utilization=0.2
    )
    candidate = CandidateMovement(sku_id=sku.sku_id, from_location=from_loc, to_location=to_loc)

    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())
    score = scorer.score(candidate, context)
    assert score == 0.0


def test_cutoff_time_approaching_increases_urgency() -> None:
    """Orders with sooner cutoffs should produce higher W_order (urgency)."""
    sku = _make_sku()
    from_loc = _make_location("FAR", x=100.0, y=50.0)
    to_loc = _make_location("STAGE", x=1.0, y=5.0, is_staging=True, nearest_dock_door=1)

    appt = _make_appointment(dock_door=1)
    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())

    # Urgent order: cutoff in 30 minutes
    urgent_order = _make_order(appt, sku.sku_id, priority=5, cutoff_hours=0.5)
    # Non-urgent order: cutoff in 8 hours
    relaxed_order = _make_order(appt, sku.sku_id, priority=5, cutoff_hours=8.0)

    state = _make_state_with_positions(sku, from_loc)

    w_urgent = scorer._compute_order_weight(urgent_order)
    w_relaxed = scorer._compute_order_weight(relaxed_order)

    assert w_urgent > w_relaxed


def test_utilization_at_cap_raises_opportunity_cost() -> None:
    """High resource utilization should produce higher opportunity cost."""
    config = _default_resource_config()
    scorer = MovementScorer(weights=ScoringWeights(), config=config)

    low_util_cost = scorer._compute_opportunity_cost(0.1)
    high_util_cost = scorer._compute_opportunity_cost(0.9)

    assert high_util_cost > low_util_cost


def test_score_components_stored_on_candidate() -> None:
    """After scoring, candidate.score_components should contain all expected keys."""
    sku = _make_sku()
    from_loc = _make_location("FAR", x=100.0, y=50.0)
    to_loc = _make_location("STAGE", x=1.0, y=5.0, is_staging=True, nearest_dock_door=1)

    appt = _make_appointment(dock_door=1)
    order = _make_order(appt, sku.sku_id)
    state = _make_state_with_positions(sku, from_loc)
    context = ScoringContext(orders=[order], appointments=[appt], resource_utilization=0.2)
    candidate = CandidateMovement(sku_id=sku.sku_id, from_location=from_loc, to_location=to_loc)

    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())
    scorer.score(candidate, context)

    assert "t_saved" in candidate.score_components
    assert "p_load" in candidate.score_components
    assert "w_order" in candidate.score_components
    assert "c_move" in candidate.score_components
    assert "c_opportunity" in candidate.score_components


def test_farther_from_dock_scores_higher_when_staged_closer() -> None:
    """A SKU that is farther from the dock should score higher when staged near it."""
    sku = _make_sku()
    # Dock door 1 at (0, 5)
    far_loc = _make_location("FAR", x=100.0, y=5.0)    # dist=100 from dock
    near_loc = _make_location("NEAR", x=20.0, y=5.0)   # dist=20 from dock
    stage_loc = _make_location("STAGE", x=1.0, y=5.0, is_staging=True, nearest_dock_door=1)

    appt = _make_appointment(dock_door=1)
    order = _make_order(appt, sku.sku_id)

    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())

    # SKU from far location → staging
    cand_far = CandidateMovement(sku_id=sku.sku_id, from_location=far_loc, to_location=stage_loc)
    state_far = _make_state_with_positions(sku, far_loc)
    ctx = ScoringContext(orders=[order], appointments=[appt], resource_utilization=0.2)
    score_far = scorer.score(cand_far, ctx)

    # SKU from near location → staging (less benefit)
    cand_near = CandidateMovement(sku_id=sku.sku_id, from_location=near_loc, to_location=stage_loc)
    state_near = _make_state_with_positions(sku, near_loc)
    score_near = scorer.score(cand_near, ctx)

    assert score_far > score_near


def test_higher_priority_order_scores_higher() -> None:
    """Higher-priority order should yield higher W_order and thus higher score."""
    sku = _make_sku()
    from_loc = _make_location("FAR", x=100.0, y=5.0)
    stage_loc = _make_location("STAGE", x=1.0, y=5.0, is_staging=True, nearest_dock_door=1)

    appt = _make_appointment(dock_door=1)
    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())

    high_priority_order = _make_order(appt, sku.sku_id, priority=9, cutoff_hours=2.0)
    low_priority_order = _make_order(appt, sku.sku_id, priority=1, cutoff_hours=2.0)

    state = _make_state_with_positions(sku, from_loc)
    ctx_high = ScoringContext(orders=[high_priority_order], appointments=[appt], resource_utilization=0.2)
    ctx_low = ScoringContext(orders=[low_priority_order], appointments=[appt], resource_utilization=0.2)

    cand = CandidateMovement(sku_id=sku.sku_id, from_location=from_loc, to_location=stage_loc)
    cand2 = CandidateMovement(sku_id=sku.sku_id, from_location=from_loc, to_location=stage_loc)

    score_high = scorer.score(cand, ctx_high)
    score_low = scorer.score(cand2, ctx_low)

    assert score_high > score_low


def test_score_positive_for_valid_movement() -> None:
    """A valid movement with matching order should produce a positive score."""
    sku = _make_sku()
    from_loc = _make_location("FAR", x=80.0, y=5.0)
    to_loc = _make_location("STAGE", x=2.0, y=5.0, is_staging=True, nearest_dock_door=1)

    appt = _make_appointment(dock_door=1)
    order = _make_order(appt, sku.sku_id)
    state = _make_state_with_positions(sku, from_loc)
    context = ScoringContext(orders=[order], appointments=[appt], resource_utilization=0.3)
    candidate = CandidateMovement(sku_id=sku.sku_id, from_location=from_loc, to_location=to_loc)

    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())
    score = scorer.score(candidate, context)
    assert score > 0.0


def test_past_cutoff_very_high_urgency() -> None:
    """An order whose cutoff has already passed should have maximum W_order."""
    sku = _make_sku()
    appt = _make_appointment(dock_door=1)

    scorer = MovementScorer(weights=ScoringWeights(), config=_default_resource_config())

    # Past-cutoff order: cutoff was 1 hour ago (negative time_until_cutoff)
    past_order = _make_order(appt, sku.sku_id, priority=5, cutoff_hours=-1.0)
    future_order = _make_order(appt, sku.sku_id, priority=5, cutoff_hours=5.0)

    w_past = scorer._compute_order_weight(past_order)
    w_future = scorer._compute_order_weight(future_order)

    # Past cutoff should have higher urgency than future
    assert w_past > w_future


def test_decay_constant_affects_urgency() -> None:
    """Decay constant controls how quickly urgency decays with time-to-cutoff.

    W_order = priority * exp(-time_until_cutoff / decay_constant).
    A LONGER decay constant means the exponential decays more slowly,
    so urgency remains higher for the same time-to-cutoff. This tests
    that the decay constant has a measurable effect on W_order.
    """
    sku = _make_sku()
    appt = _make_appointment(dock_door=1)
    # Use a cutoff 1 hour away — far enough that decay constant matters
    order = _make_order(appt, sku.sku_id, priority=5, cutoff_hours=1.0)

    # Short decay constant → urgency drops quickly → low W_order for distant cutoff
    fast_weights = ScoringWeights(decay_constant_seconds=600.0)  # 10 minutes
    # Long decay constant → urgency stays high longer → high W_order for same cutoff
    slow_weights = ScoringWeights(decay_constant_seconds=7200.0)  # 2 hours

    fast_scorer = MovementScorer(weights=fast_weights, config=_default_resource_config())
    slow_scorer = MovementScorer(weights=slow_weights, config=_default_resource_config())

    w_fast = fast_scorer._compute_order_weight(order)
    w_slow = slow_scorer._compute_order_weight(order)

    # Longer decay constant → slower decay → higher W_order when cutoff is 1h away
    assert w_slow > w_fast
