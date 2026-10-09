"""Tests for constraint filters and feasibility engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.constraints.capacity import CapacityConstraint
from src.constraints.feasibility import ConstraintFilter, FeasibilityEngine
from src.constraints.hazmat import HazmatConstraint
from src.constraints.temperature import TemperatureConstraint
from src.ingestion.wms_adapter import WarehouseState
from src.models.constraints import ConstraintSeverity, ConstraintViolation, FeasibilityResult
from src.models.inventory import (
    ABCClass,
    HazmatClass,
    InventoryPosition,
    Location,
    SKU,
    TemperatureZone,
)
from src.models.movements import CandidateMovement
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder


def _make_state(
    moving_sku: SKU,
    from_location: Location,
    to_location: Location,
    extra_positions: list[InventoryPosition] | None = None,
    location_utilization: dict[str, float] | None = None,
) -> tuple[CandidateMovement, WarehouseState]:
    """Build a candidate and minimal warehouse state for constraint tests."""
    candidate = CandidateMovement(
        sku_id=moving_sku.sku_id,
        from_location=from_location,
        to_location=to_location,
    )
    positions = [
        InventoryPosition(
            position_id="POS-TEST",
            sku=moving_sku,
            location=from_location,
            quantity=5,
        )
    ]
    if extra_positions:
        positions.extend(extra_positions)

    now = datetime.now(UTC)
    appt = CarrierAppointment(
        appointment_id="APPT-T1",
        carrier="Test Carrier",
        dock_door=1,
        scheduled_arrival=now + timedelta(hours=1),
        scheduled_departure=now + timedelta(hours=2),
        status=AppointmentStatus.SCHEDULED,
    )
    order = OutboundOrder(
        order_id="ORD-T1",
        appointment=appt,
        lines=[OrderLine(line_id="L1", sku_id=moving_sku.sku_id, quantity=1)],
        priority=5,
        cutoff_time=now + timedelta(hours=1, minutes=30),
    )

    state = WarehouseState(
        inventory_positions=positions,
        outbound_orders=[order],
        appointments=[appt],
        staging_locations=[to_location] if to_location.is_staging else [],
        resource_utilization={},
        location_utilization=location_utilization or {to_location.location_id: 0.1},
    )
    return candidate, state


# ──────────────────────────────────────────────────────────────────────────────
# Temperature constraint tests
# ──────────────────────────────────────────────────────────────────────────────

def test_ambient_sku_to_frozen_location_fails(
    sample_sku: SKU, sample_location: Location, frozen_location: Location
) -> None:
    """HARD: ambient SKU cannot go to frozen location (too warm → NO, wrong direction)."""
    # Frozen requires frozen. Ambient SKU → frozen zone is going to a COLDER zone.
    # Per spec: SKU can go to equal or colder zone. AMBIENT SKU to FROZEN = colder = OK.
    # Wait — let's re-read: "AMBIENT SKU to FROZEN" should FAIL per test name.
    # Re-read spec: "CHILLED SKU can go to FROZEN (colder is ok), but not vice versa"
    # An AMBIENT SKU to FROZEN means placing ambient product in a freezer — this is OK
    # from a preservation standpoint. BUT the spec says ambient cannot go to chilled/frozen
    # based on test name. Let's look at what makes sense:
    # The actual spec says: test_ambient_sku_to_frozen_location_fails
    # This means placing ambient product in a frozen zone is a HARD violation.
    # The temperature rule is: SKU.requires_temperature_zone must MATCH or target can be COLDER
    # BUT ambient product in frozen is problematic practically (freezes things that shouldn't be).
    # The spec explicitly says CHILLED→FROZEN OK, but ambient→frozen should fail.
    # So the rule is: only CHILLED SKU can go to FROZEN (not ambient). Ambient must go to ambient.
    candidate, state = _make_state(sample_sku, sample_location, frozen_location)
    constraint = TemperatureConstraint()
    result = constraint.check(candidate, state)
    assert not result.feasible
    assert len(result.violations) == 1
    assert result.violations[0].severity == ConstraintSeverity.HARD
    assert result.violations[0].constraint_type == "temperature"


def test_frozen_sku_to_frozen_location_passes(
    frozen_sku: SKU, sample_location: Location, frozen_location: Location
) -> None:
    """Frozen SKU to frozen location: same zone, should pass."""
    candidate, state = _make_state(frozen_sku, sample_location, frozen_location)
    constraint = TemperatureConstraint()
    result = constraint.check(candidate, state)
    assert result.feasible


def test_ambient_sku_to_ambient_location_passes(
    sample_sku: SKU, sample_location: Location, staging_location: Location
) -> None:
    """Ambient SKU to ambient location: same zone, should pass."""
    staging = Location(
        location_id="STAGE-AMB",
        zone="STAGING",
        aisle=10,
        bay=2,
        level=0,
        x=1.0,
        y=5.0,
        temperature_zone=TemperatureZone.AMBIENT,
        is_staging=True,
        nearest_dock_door=1,
    )
    candidate, state = _make_state(sample_sku, sample_location, staging)
    constraint = TemperatureConstraint()
    result = constraint.check(candidate, state)
    assert result.feasible


def test_chilled_sku_to_frozen_location_passes(
    chilled_sku: SKU, chilled_location: Location, frozen_location: Location
) -> None:
    """Chilled SKU to frozen location: colder zone is acceptable."""
    candidate, state = _make_state(chilled_sku, chilled_location, frozen_location)
    constraint = TemperatureConstraint()
    result = constraint.check(candidate, state)
    assert result.feasible


def test_frozen_sku_to_ambient_location_fails(
    frozen_sku: SKU, frozen_location: Location, sample_location: Location
) -> None:
    """HARD: frozen SKU cannot go to ambient location (too warm)."""
    candidate, state = _make_state(frozen_sku, frozen_location, sample_location)
    constraint = TemperatureConstraint()
    result = constraint.check(candidate, state)
    assert not result.feasible
    assert result.violations[0].severity == ConstraintSeverity.HARD


# ──────────────────────────────────────────────────────────────────────────────
# Hazmat constraint tests
# ──────────────────────────────────────────────────────────────────────────────

def test_hazmat_class3_next_to_class51_fails(
    hazmat_sku_class3: SKU,
    hazmat_sku_class51: SKU,
    sample_location: Location,
) -> None:
    """HARD: Class 3 flammable liquid and Class 5.1 oxidizer cannot share a bay."""
    # Class 5.1 is already in the target bay
    target_bay_location = Location(
        location_id="LOC-TARGET-BAY",
        zone="A",
        aisle=sample_location.aisle,
        bay=sample_location.bay,
        level=1,
        x=sample_location.x,
        y=sample_location.y + 2,
        temperature_zone=TemperatureZone.AMBIENT,
    )
    existing_position = InventoryPosition(
        position_id="POS-EXISTING",
        sku=hazmat_sku_class51,
        location=target_bay_location,
        quantity=5,
    )
    candidate, state = _make_state(
        hazmat_sku_class3, sample_location, target_bay_location, extra_positions=[existing_position]
    )
    constraint = HazmatConstraint()
    result = constraint.check(candidate, state)
    assert not result.feasible
    assert result.violations[0].severity == ConstraintSeverity.HARD


def test_hazmat_no_conflict_passes(
    hazmat_sku_class3: SKU,
    sample_location: Location,
) -> None:
    """Class 3 SKU moving to a bay with no hazmat: should pass."""
    target = Location(
        location_id="LOC-CLEAN-BAY",
        zone="A",
        aisle=2,
        bay=5,
        level=0,
        x=20.0,
        y=5.0,
        temperature_zone=TemperatureZone.AMBIENT,
        is_staging=True,
        nearest_dock_door=1,
    )
    candidate, state = _make_state(hazmat_sku_class3, sample_location, target)
    constraint = HazmatConstraint()
    result = constraint.check(candidate, state)
    assert result.feasible


def test_non_hazmat_sku_always_passes_hazmat_check(
    sample_sku: SKU,
    hazmat_sku_class51: SKU,
    sample_location: Location,
) -> None:
    """Non-hazmat SKU moving near hazmat: always passes hazmat constraint."""
    target_bay_location = Location(
        location_id="LOC-MIXED-BAY",
        zone="A",
        aisle=sample_location.aisle,
        bay=sample_location.bay,
        level=1,
        x=sample_location.x,
        y=sample_location.y + 2,
        temperature_zone=TemperatureZone.AMBIENT,
    )
    existing_position = InventoryPosition(
        position_id="POS-HAZ-EXISTING",
        sku=hazmat_sku_class51,
        location=target_bay_location,
        quantity=3,
    )
    candidate, state = _make_state(
        sample_sku, sample_location, target_bay_location, extra_positions=[existing_position]
    )
    constraint = HazmatConstraint()
    result = constraint.check(candidate, state)
    assert result.feasible


# ──────────────────────────────────────────────────────────────────────────────
# Capacity constraint tests
# ──────────────────────────────────────────────────────────────────────────────

def test_overweight_pallet_fails(
    sample_sku: SKU, sample_location: Location
) -> None:
    """HARD: target location at or over capacity threshold should fail."""
    target = Location(
        location_id="LOC-FULL",
        zone="A",
        aisle=3,
        bay=1,
        level=0,
        x=30.0,
        y=5.0,
        temperature_zone=TemperatureZone.AMBIENT,
        is_staging=True,
        nearest_dock_door=1,
    )
    candidate, state = _make_state(
        sample_sku,
        sample_location,
        target,
        location_utilization={target.location_id: 0.97},
    )
    constraint = CapacityConstraint(max_utilization=0.95)
    result = constraint.check(candidate, state)
    assert not result.feasible
    assert result.violations[0].severity == ConstraintSeverity.HARD


def test_within_weight_pallet_passes(
    sample_sku: SKU, sample_location: Location
) -> None:
    """Target location with sufficient capacity: should pass."""
    target = Location(
        location_id="LOC-PARTIAL",
        zone="A",
        aisle=3,
        bay=2,
        level=0,
        x=30.0,
        y=10.0,
        temperature_zone=TemperatureZone.AMBIENT,
        is_staging=True,
        nearest_dock_door=1,
    )
    candidate, state = _make_state(
        sample_sku,
        sample_location,
        target,
        location_utilization={target.location_id: 0.50},
    )
    constraint = CapacityConstraint(max_utilization=0.95)
    result = constraint.check(candidate, state)
    assert result.feasible


def test_full_location_fails_capacity(
    sample_sku: SKU, sample_location: Location
) -> None:
    """HARD: location at exactly 1.0 utilization should be rejected."""
    target = Location(
        location_id="LOC-COMPLETELY-FULL",
        zone="A",
        aisle=3,
        bay=3,
        level=0,
        x=30.0,
        y=15.0,
        temperature_zone=TemperatureZone.AMBIENT,
    )
    candidate, state = _make_state(
        sample_sku,
        sample_location,
        target,
        location_utilization={target.location_id: 1.0},
    )
    constraint = CapacityConstraint(max_utilization=0.95)
    result = constraint.check(candidate, state)
    assert not result.feasible


# ──────────────────────────────────────────────────────────────────────────────
# FeasibilityEngine tests
# ──────────────────────────────────────────────────────────────────────────────

def test_feasibility_engine_stops_on_first_hard_violation(
    frozen_sku: SKU,
    frozen_location: Location,
    sample_location: Location,
) -> None:
    """Engine stops evaluating on first HARD violation."""
    # frozen_sku → ambient location is a HARD temperature violation
    candidate, state = _make_state(frozen_sku, frozen_location, sample_location)
    # Add full location to also trigger capacity violation — but engine should stop at temp
    state.location_utilization[sample_location.location_id] = 1.0

    engine = FeasibilityEngine(
        filters=[
            TemperatureConstraint(),
            CapacityConstraint(),
        ]
    )
    result = engine.evaluate(candidate, state)
    assert not result.feasible
    # Should have exactly 1 violation (stopped at first HARD)
    assert len(result.violations) == 1
    assert result.violations[0].constraint_type == "temperature"


def test_feasibility_engine_collects_soft_violations(
    sample_sku: SKU,
    sample_location: Location,
) -> None:
    """Engine collects soft violations and still returns feasible=True."""

    class AlwaysSoftFilter(ConstraintFilter):
        def check(
            self, movement: CandidateMovement, state: WarehouseState
        ) -> FeasibilityResult:
            return FeasibilityResult(
                feasible=True,
                violations=[
                    ConstraintViolation(
                        constraint_type="soft_test",
                        description="Soft advisory violation.",
                        severity=ConstraintSeverity.SOFT,
                    )
                ],
            )

    target = Location(
        location_id="LOC-SOFT-TEST",
        zone="A",
        aisle=1,
        bay=9,
        level=0,
        x=5.0,
        y=2.0,
        temperature_zone=TemperatureZone.AMBIENT,
    )
    candidate, state = _make_state(sample_sku, sample_location, target)
    engine = FeasibilityEngine(
        filters=[AlwaysSoftFilter(), AlwaysSoftFilter()]
    )
    result = engine.evaluate(candidate, state)
    assert result.feasible
    assert len(result.violations) == 2
    assert all(v.severity == ConstraintSeverity.SOFT for v in result.violations)
