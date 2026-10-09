"""Shared pytest fixtures for all test modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.ingestion.wms_adapter import WarehouseState
from src.models.inventory import ABCClass, HazmatClass, InventoryPosition, Location, SKU, TemperatureZone
from src.models.movements import CandidateMovement
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.scoring.weights import ScoringWeights


@pytest.fixture
def sample_location() -> Location:
    """Return an ambient staging location near dock door 1."""
    return Location(
        location_id="LOC-A1-01",
        zone="A",
        aisle=1,
        bay=1,
        level=0,
        x=10.0,
        y=5.0,
        temperature_zone=TemperatureZone.AMBIENT,
        max_weight_kg=2000.0,
        max_volume_m3=10.0,
        is_staging=False,
        nearest_dock_door=None,
    )


@pytest.fixture
def staging_location() -> Location:
    """Return a staging location assigned to dock door 1."""
    return Location(
        location_id="STAGE-D1-01",
        zone="STAGING",
        aisle=10,
        bay=1,
        level=0,
        x=2.0,
        y=5.0,
        temperature_zone=TemperatureZone.AMBIENT,
        max_weight_kg=2000.0,
        max_volume_m3=10.0,
        is_staging=True,
        nearest_dock_door=1,
    )


@pytest.fixture
def frozen_location() -> Location:
    """Return a frozen-zone storage location."""
    return Location(
        location_id="LOC-FROZEN-01",
        zone="COLD",
        aisle=5,
        bay=1,
        level=0,
        x=50.0,
        y=5.0,
        temperature_zone=TemperatureZone.FROZEN,
        max_weight_kg=1500.0,
        max_volume_m3=8.0,
        is_staging=False,
        nearest_dock_door=None,
    )


@pytest.fixture
def chilled_location() -> Location:
    """Return a chilled-zone storage location."""
    return Location(
        location_id="LOC-CHILLED-01",
        zone="COLD",
        aisle=4,
        bay=1,
        level=0,
        x=40.0,
        y=5.0,
        temperature_zone=TemperatureZone.CHILLED,
        max_weight_kg=1500.0,
        max_volume_m3=8.0,
        is_staging=False,
        nearest_dock_door=None,
    )


@pytest.fixture
def sample_sku() -> SKU:
    """Return a standard ambient SKU with Class A velocity."""
    return SKU(
        sku_id="SKU-AMBIENT-001",
        description="Standard ambient product",
        weight_kg=100.0,
        volume_m3=0.5,
        hazmat_class=None,
        requires_temperature_zone=TemperatureZone.AMBIENT,
        abc_class=ABCClass.A,
    )


@pytest.fixture
def frozen_sku() -> SKU:
    """Return a SKU requiring frozen storage."""
    return SKU(
        sku_id="SKU-FROZEN-001",
        description="Frozen food product",
        weight_kg=50.0,
        volume_m3=0.3,
        hazmat_class=None,
        requires_temperature_zone=TemperatureZone.FROZEN,
        abc_class=ABCClass.B,
    )


@pytest.fixture
def chilled_sku() -> SKU:
    """Return a SKU requiring chilled (not frozen) storage."""
    return SKU(
        sku_id="SKU-CHILLED-001",
        description="Refrigerated dairy product",
        weight_kg=20.0,
        volume_m3=0.1,
        hazmat_class=None,
        requires_temperature_zone=TemperatureZone.CHILLED,
        abc_class=ABCClass.B,
    )


@pytest.fixture
def hazmat_sku_class3() -> SKU:
    """Return a Class 3 (flammable liquid) hazmat SKU."""
    return SKU(
        sku_id="SKU-HAZMAT-CLASS3",
        description="Flammable solvent",
        weight_kg=30.0,
        volume_m3=0.2,
        hazmat_class=HazmatClass.CLASS_3,
        requires_temperature_zone=TemperatureZone.AMBIENT,
        abc_class=ABCClass.C,
    )


@pytest.fixture
def hazmat_sku_class51() -> SKU:
    """Return a Class 5.1 (oxidizer) hazmat SKU."""
    return SKU(
        sku_id="SKU-HAZMAT-CLASS51",
        description="Industrial oxidizer",
        weight_kg=25.0,
        volume_m3=0.15,
        hazmat_class=HazmatClass.CLASS_5_1,
        requires_temperature_zone=TemperatureZone.AMBIENT,
        abc_class=ABCClass.C,
    )


@pytest.fixture
def sample_appointment() -> CarrierAppointment:
    """Return a carrier appointment 2 hours in the future."""
    now = datetime.now(UTC)
    return CarrierAppointment(
        appointment_id="APPT-001",
        carrier="ACME Freight",
        dock_door=1,
        scheduled_arrival=now + timedelta(hours=2),
        scheduled_departure=now + timedelta(hours=3),
        status=AppointmentStatus.SCHEDULED,
    )


@pytest.fixture
def sample_order(sample_appointment: CarrierAppointment, sample_sku: SKU) -> OutboundOrder:
    """Return an outbound order with one line for sample_sku."""
    now = datetime.now(UTC)
    return OutboundOrder(
        order_id="ORD-001",
        appointment=sample_appointment,
        lines=[
            OrderLine(
                line_id="LINE-001",
                sku_id=sample_sku.sku_id,
                quantity=10,
                picked=False,
            )
        ],
        priority=5,
        cutoff_time=now + timedelta(hours=2, minutes=30),
    )


@pytest.fixture
def sample_candidate(sample_sku: SKU, sample_location: Location, staging_location: Location) -> CandidateMovement:
    """Return a candidate movement for sample_sku from sample_location to staging_location."""
    return CandidateMovement(
        sku_id=sample_sku.sku_id,
        from_location=sample_location,
        to_location=staging_location,
        reason="Test candidate",
    )


@pytest.fixture
def scoring_weights() -> ScoringWeights:
    """Return default ScoringWeights."""
    return ScoringWeights()


@pytest.fixture
def warehouse_state(
    sample_sku: SKU,
    frozen_sku: SKU,
    sample_location: Location,
    frozen_location: Location,
    staging_location: Location,
    sample_appointment: CarrierAppointment,
    sample_order: OutboundOrder,
) -> WarehouseState:
    """Return a WarehouseState with sample inventory, orders, and appointments."""
    return WarehouseState(
        inventory_positions=[
            InventoryPosition(
                position_id="POS-001",
                sku=sample_sku,
                location=sample_location,
                quantity=20,
            ),
            InventoryPosition(
                position_id="POS-002",
                sku=frozen_sku,
                location=frozen_location,
                quantity=10,
            ),
        ],
        outbound_orders=[sample_order],
        appointments=[sample_appointment],
        staging_locations=[staging_location],
        resource_utilization={"AGV-1": 0.3, "FORKLIFT-1": 0.5},
        location_utilization={
            sample_location.location_id: 0.4,
            frozen_location.location_id: 0.2,
            staging_location.location_id: 0.1,
        },
    )
