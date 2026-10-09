"""Integration tests using in-memory stub adapters — no DB or Redis required."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import ResourceConfig
from src.constraints.capacity import CapacityConstraint
from src.constraints.feasibility import FeasibilityEngine
from src.constraints.hazmat import HazmatConstraint
from src.constraints.temperature import TemperatureConstraint
from src.dispatch.task_queue import TaskQueue
from src.ingestion.wms_adapter import WMSAdapter, WarehouseState
from src.models.inventory import ABCClass, InventoryPosition, Location, SKU, TemperatureZone
from src.models.movements import MovementStatus, MovementTask
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.optimizer.scheduler import PrePositionScheduler, SchedulerConfig
from src.scoring.value_function import MovementScorer
from src.scoring.weights import ScoringWeights


# ──────────────────────────────────────────────────────────────────────────────
# In-memory stub adapter
# ──────────────────────────────────────────────────────────────────────────────

class InMemoryWMSAdapter(WMSAdapter):
    """WMS adapter backed by in-memory data for integration tests."""

    def __init__(self, state: WarehouseState) -> None:
        self._state = state

    async def get_inventory_positions(
        self, zone: str | None = None
    ) -> list[InventoryPosition]:
        if zone:
            return [p for p in self._state.inventory_positions if p.location.zone == zone]
        return list(self._state.inventory_positions)

    async def get_outbound_orders(
        self, horizon_hours: float = 24
    ) -> list[OutboundOrder]:
        return list(self._state.outbound_orders)

    async def get_carrier_appointments(
        self, horizon_hours: float = 24
    ) -> list[CarrierAppointment]:
        return list(self._state.appointments)

    async def get_staging_locations(
        self, dock_door: int | None = None
    ) -> list[Location]:
        if dock_door is not None:
            return [loc for loc in self._state.staging_locations if loc.nearest_dock_door == dock_door]
        return list(self._state.staging_locations)

    async def get_location_utilization(self) -> dict[str, float]:
        return dict(self._state.location_utilization)


# ──────────────────────────────────────────────────────────────────────────────
# In-memory stub task queue
# ──────────────────────────────────────────────────────────────────────────────

class InMemoryTaskQueue(TaskQueue):
    """In-memory task queue for integration tests (no Redis)."""

    def __init__(self) -> None:
        self._tasks: dict[str, MovementTask] = {}

    async def push(self, task: MovementTask) -> None:
        self._tasks[str(task.movement_id)] = task

    async def pop(self, n: int = 1) -> list[MovementTask]:
        sorted_tasks = sorted(self._tasks.values(), key=lambda t: t.score, reverse=True)
        return sorted_tasks[:n]

    async def update_status(self, movement_id: str, status: MovementStatus) -> None:
        if movement_id in self._tasks:
            task = self._tasks[movement_id]
            self._tasks[movement_id] = task.model_copy(update={"status": status})

    async def get_active_tasks(self) -> list[MovementTask]:
        return list(self._tasks.values())

    async def expire_old_tasks(self, expiry_minutes: int | None = None) -> int:
        return 0

    async def get_queue_depth(self) -> int:
        return len(self._tasks)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _make_loc(loc_id: str, x: float, y: float, zone: str = "A", **kwargs: object) -> Location:
    return Location(
        location_id=loc_id,
        zone=zone,
        aisle=1,
        bay=1,
        level=0,
        x=x,
        y=y,
        temperature_zone=TemperatureZone.AMBIENT,
        **kwargs,  # type: ignore[arg-type]
    )


def _make_sku(sku_id: str, zone: TemperatureZone = TemperatureZone.AMBIENT) -> SKU:
    return SKU(
        sku_id=sku_id,
        description=f"Test SKU {sku_id}",
        weight_kg=50.0,
        volume_m3=0.3,
        abc_class=ABCClass.A,
        requires_temperature_zone=zone,
    )


@pytest.fixture
def rich_warehouse_state() -> WarehouseState:
    """Build a warehouse state with 10 positions, 3 orders, 2 appointments."""
    now = datetime.now(UTC)

    # Staging locations near dock doors 1 and 2
    staging_d1 = _make_loc("STAGE-D1", x=0.0, y=5.0, zone="STAGING", is_staging=True, nearest_dock_door=1)
    staging_d2 = _make_loc("STAGE-D2", x=0.0, y=10.0, zone="STAGING", is_staging=True, nearest_dock_door=2)

    # Regular storage locations (further from dock)
    regular_locs = [
        _make_loc(f"LOC-{i:02d}", x=float(20 + i * 5), y=5.0, zone="A")
        for i in range(8)
    ]
    # One frozen location
    frozen_loc = Location(
        location_id="LOC-FROZEN",
        zone="COLD",
        aisle=9,
        bay=1,
        level=0,
        x=90.0,
        y=5.0,
        temperature_zone=TemperatureZone.FROZEN,
    )
    # One ambient location that would be wrong for frozen SKU
    ambient_loc_for_frozen = _make_loc("LOC-AMB-WRONG", x=50.0, y=5.0, zone="A")

    # SKUs: 8 ambient, 1 frozen, 1 frozen (that will try to go to ambient)
    ambient_skus = [_make_sku(f"SKU-{i:03d}") for i in range(8)]
    frozen_sku = _make_sku("SKU-FROZEN-001", zone=TemperatureZone.FROZEN)
    frozen_sku_2 = _make_sku("SKU-FROZEN-002", zone=TemperatureZone.FROZEN)

    # Appointments
    appt1 = CarrierAppointment(
        appointment_id="APPT-001",
        carrier="Carrier A",
        dock_door=1,
        scheduled_arrival=now + timedelta(hours=2),
        scheduled_departure=now + timedelta(hours=3),
        status=AppointmentStatus.SCHEDULED,
    )
    appt2 = CarrierAppointment(
        appointment_id="APPT-002",
        carrier="Carrier B",
        dock_door=2,
        scheduled_arrival=now + timedelta(hours=4),
        scheduled_departure=now + timedelta(hours=5),
        status=AppointmentStatus.SCHEDULED,
    )

    # Orders: 3 orders covering different SKUs
    order1 = OutboundOrder(
        order_id="ORD-001",
        appointment=appt1,
        lines=[
            OrderLine(line_id="L01", sku_id="SKU-000", quantity=5),
            OrderLine(line_id="L02", sku_id="SKU-001", quantity=3),
            OrderLine(line_id="L03", sku_id="SKU-002", quantity=8),
        ],
        priority=8,
        cutoff_time=now + timedelta(hours=2, minutes=30),
    )
    order2 = OutboundOrder(
        order_id="ORD-002",
        appointment=appt1,
        lines=[
            OrderLine(line_id="L04", sku_id="SKU-003", quantity=2),
            OrderLine(line_id="L05", sku_id="SKU-004", quantity=6),
        ],
        priority=5,
        cutoff_time=now + timedelta(hours=2, minutes=45),
    )
    order3 = OutboundOrder(
        order_id="ORD-003",
        appointment=appt2,
        lines=[
            OrderLine(line_id="L06", sku_id="SKU-005", quantity=4),
            OrderLine(line_id="L07", sku_id="SKU-006", quantity=1),
        ],
        priority=3,
        cutoff_time=now + timedelta(hours=4, minutes=30),
    )

    # Build inventory positions
    positions: list[InventoryPosition] = []
    for i, (sku, loc) in enumerate(zip(ambient_skus, regular_locs)):
        positions.append(
            InventoryPosition(
                position_id=f"POS-{i:03d}",
                sku=sku,
                location=loc,
                quantity=10,
            )
        )
    # Frozen SKU in frozen location (valid)
    positions.append(
        InventoryPosition(
            position_id="POS-FROZEN-1",
            sku=frozen_sku,
            location=frozen_loc,
            quantity=5,
        )
    )
    # Frozen SKU placed in ambient location — would be infeasible for ambient staging
    positions.append(
        InventoryPosition(
            position_id="POS-FROZEN-2",
            sku=frozen_sku_2,
            location=ambient_loc_for_frozen,
            quantity=3,
        )
    )

    # Location utilization (all low)
    utilization: dict[str, float] = {
        staging_d1.location_id: 0.1,
        staging_d2.location_id: 0.1,
        frozen_loc.location_id: 0.2,
        ambient_loc_for_frozen.location_id: 0.3,
    }
    for loc in regular_locs:
        utilization[loc.location_id] = 0.4

    return WarehouseState(
        inventory_positions=positions,
        outbound_orders=[order1, order2, order3],
        appointments=[appt1, appt2],
        staging_locations=[staging_d1, staging_d2],
        resource_utilization={"AGV-1": 0.3},
        location_utilization=utilization,
    )


@pytest.fixture
def scheduler(rich_warehouse_state: WarehouseState) -> PrePositionScheduler:
    """Build a PrePositionScheduler with in-memory stubs."""
    wms = InMemoryWMSAdapter(rich_warehouse_state)
    task_queue = InMemoryTaskQueue()

    weights = ScoringWeights()
    resource_config = ResourceConfig(
        forklift_speed_mps=2.2,
        agv_speed_mps=1.3,
        handling_time_seconds=45.0,
        max_utilization=0.95,
        base_opportunity_seconds=60.0,
    )
    scorer = MovementScorer(weights=weights, config=resource_config)
    feasibility = FeasibilityEngine(
        filters=[TemperatureConstraint(), HazmatConstraint(), CapacityConstraint()]
    )
    config = SchedulerConfig(
        cycle_interval_seconds=60,
        dispatch_batch_size=5,
        horizon_hours=24,
        max_candidates=50,
        min_score_threshold=0.0,
    )

    return PrePositionScheduler(
        scorer=scorer,
        feasibility=feasibility,
        wms=wms,
        task_queue=task_queue,
        config=config,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Integration tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_candidates_generated_with_positive_scores(
    scheduler: PrePositionScheduler,
) -> None:
    """Full pipeline produces at least some candidates with positive scores."""
    candidates = await scheduler.generate_candidates()
    assert len(candidates) > 0
    assert all(c.score >= 0.0 for c in candidates)
    # At least some should be positive
    positive = [c for c in candidates if c.score > 0.0]
    assert len(positive) > 0


@pytest.mark.asyncio
async def test_all_candidates_passed_feasibility(
    scheduler: PrePositionScheduler,
    rich_warehouse_state: WarehouseState,
) -> None:
    """All returned candidates must have passed the feasibility engine."""
    candidates = await scheduler.generate_candidates()
    feasibility = FeasibilityEngine(
        filters=[TemperatureConstraint(), HazmatConstraint(), CapacityConstraint()]
    )
    for candidate in candidates:
        result = feasibility.evaluate(candidate, rich_warehouse_state)
        assert result.feasible, (
            f"Candidate {candidate.movement_id} (SKU {candidate.sku_id}) "
            f"failed feasibility: {result.violations}"
        )


@pytest.mark.asyncio
async def test_top_candidate_has_highest_score(
    scheduler: PrePositionScheduler,
) -> None:
    """Candidates are returned in descending score order."""
    candidates = await scheduler.generate_candidates()
    if len(candidates) >= 2:
        scores = [c.score for c in candidates]
        assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_frozen_sku_not_in_ambient_staging_candidates(
    scheduler: PrePositionScheduler,
    rich_warehouse_state: WarehouseState,
) -> None:
    """Frozen SKUs should not appear as candidates targeting ambient staging locations."""
    candidates = await scheduler.generate_candidates()

    # Find all frozen SKU IDs
    frozen_sku_ids = {
        pos.sku.sku_id
        for pos in rich_warehouse_state.inventory_positions
        if pos.sku.requires_temperature_zone == TemperatureZone.FROZEN
    }

    # Verify no frozen SKU is targeted at an ambient staging location
    for candidate in candidates:
        if candidate.sku_id in frozen_sku_ids:
            target_zone = candidate.to_location.temperature_zone
            assert target_zone == TemperatureZone.FROZEN, (
                f"Frozen SKU {candidate.sku_id} was assigned to "
                f"{target_zone.value} staging location {candidate.to_location.location_id}"
            )


@pytest.mark.asyncio
async def test_score_components_populated_on_all_candidates(
    scheduler: PrePositionScheduler,
) -> None:
    """All returned candidates must have score_components populated."""
    candidates = await scheduler.generate_candidates()
    for candidate in candidates:
        assert candidate.score_components, (
            f"Candidate {candidate.movement_id} has empty score_components"
        )
        assert "t_saved" in candidate.score_components
        assert "p_load" in candidate.score_components
        assert "w_order" in candidate.score_components
        assert "c_move" in candidate.score_components
        assert "c_opportunity" in candidate.score_components


@pytest.mark.asyncio
async def test_full_cycle_dispatches_tasks(
    scheduler: PrePositionScheduler,
) -> None:
    """A full scheduling cycle dispatches tasks from the top candidates."""
    candidates, tasks = await scheduler.run_cycle()
    assert len(candidates) > 0
    assert len(tasks) <= len(candidates)
    assert len(tasks) <= scheduler._config.dispatch_batch_size


@pytest.mark.asyncio
async def test_scheduling_cycle_respects_min_score_threshold() -> None:
    """Candidates below min_score_threshold are excluded."""
    now = datetime.now(UTC)

    # One staging location
    staging = Location(
        location_id="STAGE-D1",
        zone="STAGING",
        aisle=10,
        bay=1,
        level=0,
        x=0.0,
        y=5.0,
        temperature_zone=TemperatureZone.AMBIENT,
        is_staging=True,
        nearest_dock_door=1,
    )
    sku = _make_sku("SKU-TEST")
    # SKU far from dock — should produce non-zero score
    far_loc = _make_loc("FAR", x=100.0, y=5.0)

    appt = CarrierAppointment(
        appointment_id="APPT-THRESHOLD",
        carrier="Test",
        dock_door=1,
        scheduled_arrival=now + timedelta(hours=2),
        scheduled_departure=now + timedelta(hours=3),
        status=AppointmentStatus.SCHEDULED,
    )
    order = OutboundOrder(
        order_id="ORD-THRESH",
        appointment=appt,
        lines=[OrderLine(line_id="L1", sku_id=sku.sku_id, quantity=5)],
        priority=5,
        cutoff_time=now + timedelta(hours=2, minutes=30),
    )

    state = WarehouseState(
        inventory_positions=[
            InventoryPosition(position_id="POS-T1", sku=sku, location=far_loc, quantity=5)
        ],
        outbound_orders=[order],
        appointments=[appt],
        staging_locations=[staging],
        resource_utilization={},
        location_utilization={staging.location_id: 0.1, far_loc.location_id: 0.3},
    )

    wms = InMemoryWMSAdapter(state)
    task_queue = InMemoryTaskQueue()
    scorer = MovementScorer(
        weights=ScoringWeights(),
        config=ResourceConfig(forklift_speed_mps=2.2, agv_speed_mps=1.3, handling_time_seconds=45.0),
    )
    feasibility = FeasibilityEngine(
        filters=[TemperatureConstraint(), CapacityConstraint()]
    )

    # Very high threshold to exclude everything
    config_high = SchedulerConfig(min_score_threshold=999999.0)
    scheduler_high = PrePositionScheduler(
        scorer=scorer, feasibility=feasibility, wms=wms, task_queue=task_queue, config=config_high
    )
    candidates_high = await scheduler_high.generate_candidates()
    assert len(candidates_high) == 0

    # Zero threshold to include everything
    config_low = SchedulerConfig(min_score_threshold=0.0)
    scheduler_low = PrePositionScheduler(
        scorer=scorer, feasibility=feasibility, wms=wms, task_queue=task_queue, config=config_low
    )
    candidates_low = await scheduler_low.generate_candidates()
    # Should have at least one positive-score candidate
    assert any(c.score > 0.0 for c in candidates_low)
