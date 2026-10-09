"""Tests for Phase 3 OR-Tools assignment and routing components."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from src.models.inventory import (
    ABCClass,
    HazmatClass,
    InventoryPosition,
    Location,
    SKU,
    TemperatureZone,
)
from src.models.movements import CandidateMovement, MovementTask
from src.optimizer.assignment import (
    AssignmentResult,
    StagingAssignmentSolver,
    _hazmat_incompatible,
    _temperature_compatible,
)
from src.optimizer.routing import (
    GraphEdge,
    MovementRoutePlanner,
    Route,
    RoutingResult,
    WarehouseGraph,
    _build_time_matrix,
    _manhattan,
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _loc(
    location_id: str,
    x: float = 0.0,
    y: float = 0.0,
    zone: TemperatureZone = TemperatureZone.AMBIENT,
    aisle: int = 1,
    is_staging: bool = False,
    dock_door: int = 1,
) -> Location:
    return Location(
        location_id=location_id,
        zone="BULK",
        aisle=aisle,
        bay=1,
        level=0,
        x=x,
        y=y,
        temperature_zone=zone,
        max_weight_kg=1000.0,
        max_volume_m3=10.0,
        is_staging=is_staging,
        nearest_dock_door=dock_door,
    )


def _candidate(
    sku_id: str = "SKU-1",
    score: float = 1.0,
    from_loc: Location | None = None,
    to_loc: Location | None = None,
) -> CandidateMovement:
    return CandidateMovement(
        sku_id=sku_id,
        from_location=from_loc or _loc("FROM-1", x=10.0, y=10.0),
        to_location=to_loc or _loc("STAGE-1", x=5.0, y=5.0, is_staging=True),
        score=score,
        reason="test",
    )


def _task(
    sku_id: str = "SKU-1",
    score: float = 1.0,
    from_loc: Location | None = None,
) -> MovementTask:
    return MovementTask(
        sku_id=sku_id,
        from_location=from_loc or _loc("FROM-1", x=10.0, y=10.0),
        to_location=_loc("STAGE-1", x=5.0, y=5.0, is_staging=True),
        score=score,
        reason="test",
        assigned_resource="AGV-1",
        dispatched_at=datetime.now(UTC),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Assignment: helper functions
# ──────────────────────────────────────────────────────────────────────────────


def test_temperature_compatible_same_zone() -> None:
    assert _temperature_compatible(TemperatureZone.AMBIENT, TemperatureZone.AMBIENT)


def test_temperature_compatible_chilled_in_frozen() -> None:
    assert _temperature_compatible(TemperatureZone.CHILLED, TemperatureZone.FROZEN)


def test_temperature_incompatible_ambient_in_frozen() -> None:
    assert not _temperature_compatible(TemperatureZone.AMBIENT, TemperatureZone.FROZEN)


def test_temperature_incompatible_frozen_in_ambient() -> None:
    assert not _temperature_compatible(TemperatureZone.FROZEN, TemperatureZone.AMBIENT)


def test_hazmat_incompatible_class3_class5_1() -> None:
    assert _hazmat_incompatible("CLASS_3", "CLASS_5_1")


def test_hazmat_incompatible_symmetric() -> None:
    """Incompatibility is symmetric — order should not matter."""
    assert _hazmat_incompatible("CLASS_5_1", "CLASS_3") == _hazmat_incompatible("CLASS_3", "CLASS_5_1")


def test_hazmat_compatible_nones() -> None:
    assert not _hazmat_incompatible(None, None)
    assert not _hazmat_incompatible("CLASS_3", None)


# ──────────────────────────────────────────────────────────────────────────────
# Assignment: StagingAssignmentSolver
# ──────────────────────────────────────────────────────────────────────────────


def test_assignment_empty_candidates_returns_infeasible() -> None:
    solver = StagingAssignmentSolver()
    result = solver.solve([], [_loc("S1", is_staging=True)], available_resources=2)
    assert result.solver_status == "INFEASIBLE"
    assert result.tasks == []


def test_assignment_empty_staging_returns_infeasible() -> None:
    solver = StagingAssignmentSolver()
    result = solver.solve([_candidate()], [], available_resources=2)
    assert result.solver_status == "INFEASIBLE"


def test_assignment_single_candidate_single_location() -> None:
    """One candidate, one location — solver should assign it."""
    solver = StagingAssignmentSolver(max_staging_distance_meters=200.0)
    staging = _loc("STAGE-1", x=5.0, y=5.0, is_staging=True)
    cand = _candidate(score=2.5, from_loc=_loc("FROM-1", x=10.0, y=10.0), to_loc=staging)

    result = solver.solve([cand], [staging], available_resources=1)

    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert len(result.tasks) == 1
    assert result.tasks[0].sku_id == "SKU-1"


def test_assignment_resource_budget_limits_output() -> None:
    """When available_resources=1, at most one task is dispatched."""
    solver = StagingAssignmentSolver(max_staging_distance_meters=200.0)
    staging_locs = [_loc(f"S{i}", x=float(i), y=0.0, is_staging=True) for i in range(3)]
    candidates = [
        _candidate(sku_id=f"SKU-{i}", score=float(i + 1),
                   from_loc=_loc(f"F{i}", x=float(i + 5), y=0.0),
                   to_loc=staging_locs[i])
        for i in range(3)
    ]

    result = solver.solve(candidates, staging_locs, available_resources=1)

    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert len(result.tasks) <= 1


def test_assignment_each_location_at_most_one_pallet() -> None:
    """Two candidates targeting the same location — only one gets assigned."""
    solver = StagingAssignmentSolver(max_staging_distance_meters=200.0)
    staging = _loc("STAGE-1", x=1.0, y=1.0, is_staging=True)
    c1 = _candidate(sku_id="SKU-A", score=3.0,
                    from_loc=_loc("FA", x=5.0, y=5.0), to_loc=staging)
    c2 = _candidate(sku_id="SKU-B", score=2.0,
                    from_loc=_loc("FB", x=6.0, y=6.0), to_loc=staging)

    result = solver.solve([c1, c2], [staging], available_resources=2)

    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert len(result.tasks) <= 1


def test_assignment_prefers_higher_score() -> None:
    """Solver should select the higher-scored candidate when budget=1."""
    solver = StagingAssignmentSolver(max_staging_distance_meters=200.0)
    s1 = _loc("S1", x=1.0, y=0.0, is_staging=True, aisle=1)
    s2 = _loc("S2", x=2.0, y=0.0, is_staging=True, aisle=2)
    low = _candidate(sku_id="LOW", score=1.0, from_loc=_loc("FL", x=5.0, y=0.0), to_loc=s1)
    high = _candidate(sku_id="HIGH", score=5.0, from_loc=_loc("FH", x=6.0, y=0.0), to_loc=s2)

    result = solver.solve([low, high], [s1, s2], available_resources=1)

    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert len(result.tasks) == 1
    assert result.tasks[0].sku_id == "HIGH"


def test_assignment_temperature_filter_excludes_incompatible() -> None:
    """FROZEN SKU must not be assigned to an AMBIENT staging location."""
    solver = StagingAssignmentSolver(max_staging_distance_meters=200.0)
    from_loc = _loc("FROM", x=10.0, y=10.0, zone=TemperatureZone.FROZEN)
    staging_ambient = _loc("STAGE-AMB", x=5.0, y=5.0, zone=TemperatureZone.AMBIENT, is_staging=True)

    cand = _candidate(from_loc=from_loc, to_loc=staging_ambient, score=5.0)
    result = solver.solve([cand], [staging_ambient], available_resources=2)

    # No feasible pairs — should return INFEASIBLE or zero tasks.
    assert result.tasks == [] or result.solver_status == "INFEASIBLE"


def test_assignment_result_has_or_annotation() -> None:
    """Assigned tasks should have the OR-Tools location annotation appended."""
    solver = StagingAssignmentSolver(max_staging_distance_meters=200.0)
    staging = _loc("STAGE-X", x=3.0, y=3.0, is_staging=True)
    cand = _candidate(score=2.0, from_loc=_loc("FROM", x=5.0, y=5.0), to_loc=staging)

    result = solver.solve([cand], [staging], available_resources=1)

    if result.tasks:
        assert "OR-assigned" in result.tasks[0].reason


# ──────────────────────────────────────────────────────────────────────────────
# Routing: WarehouseGraph
# ──────────────────────────────────────────────────────────────────────────────


def test_warehouse_graph_add_edge_bidirectional() -> None:
    g = WarehouseGraph()
    g.add_edge(GraphEdge(from_node="A", to_node="B", distance_meters=10.0, speed_mps=2.0))
    assert g.has_edge("A", "B")
    assert g.has_edge("B", "A")


def test_warehouse_graph_one_way_not_reversed() -> None:
    g = WarehouseGraph()
    g.add_edge(GraphEdge(from_node="A", to_node="B", distance_meters=10.0, one_way=True))
    assert g.has_edge("A", "B")
    assert not g.has_edge("B", "A")


def test_warehouse_graph_travel_time() -> None:
    g = WarehouseGraph()
    g.add_edge(GraphEdge(from_node="X", to_node="Y", distance_meters=22.0, speed_mps=2.2))
    t = g.travel_time_seconds("X", "Y")
    assert abs(t - 10.0) < 0.01  # 22 / 2.2 = 10.0s


def test_warehouse_graph_no_edge_returns_zero() -> None:
    g = WarehouseGraph()
    assert g.travel_time_seconds("A", "B") == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Routing: time matrix helper
# ──────────────────────────────────────────────────────────────────────────────


def test_build_time_matrix_diagonal_zero() -> None:
    locs = [_loc("L1", x=0.0, y=0.0), _loc("L2", x=10.0, y=0.0)]
    matrix = _build_time_matrix(locs, None, speed_mps=2.0)
    assert matrix[0][0] == 0
    assert matrix[1][1] == 0


def test_build_time_matrix_symmetric_without_graph() -> None:
    locs = [_loc("L1", x=0.0, y=0.0), _loc("L2", x=10.0, y=5.0)]
    matrix = _build_time_matrix(locs, None, speed_mps=1.0)
    # Distance = |0-10| + |0-5| = 15; time = 15s
    assert matrix[0][1] == matrix[1][0]
    assert matrix[0][1] >= 1  # at least 1


# ──────────────────────────────────────────────────────────────────────────────
# Routing: MovementRoutePlanner
# ──────────────────────────────────────────────────────────────────────────────


def test_routing_empty_tasks_returns_infeasible() -> None:
    planner = MovementRoutePlanner()
    result = planner.plan([], ["AGV-1"])
    assert result.solver_status == "INFEASIBLE"
    assert result.routes == []


def test_routing_empty_resources_returns_infeasible() -> None:
    planner = MovementRoutePlanner()
    result = planner.plan([_task()], [])
    assert result.solver_status == "INFEASIBLE"


def test_routing_single_task_single_resource() -> None:
    """One task, one resource — expect a route with one stop."""
    planner = MovementRoutePlanner(solver_timeout_seconds=5)
    t = _task(score=2.0, from_loc=_loc("FROM", x=10.0, y=10.0))
    result = planner.plan([t], ["AGV-1"], time_horizon_seconds=3600)

    # Accept any successful status (exact string varies by OR-Tools version).
    assert "FAIL" not in result.solver_status and "INFEASIBLE" not in result.solver_status
    assert len(result.routes) == 1
    assert len(result.routes[0].stops) == 1
    assert result.routes[0].resource_id == "AGV-1"


def test_routing_multiple_tasks_multiple_resources() -> None:
    """Three tasks, two resources — at least one stop should be planned."""
    planner = MovementRoutePlanner(solver_timeout_seconds=5)
    tasks = [
        _task(sku_id=f"SKU-{i}", score=float(i + 1),
              from_loc=_loc(f"FROM-{i}", x=float(i * 5), y=0.0))
        for i in range(3)
    ]
    result = planner.plan(tasks, ["AGV-1", "AGV-2"], time_horizon_seconds=3600)

    assert "FAIL" not in result.solver_status and "INFEASIBLE" not in result.solver_status
    total_stops = sum(len(r.stops) for r in result.routes)
    assert total_stops >= 1


def test_routing_with_warehouse_graph() -> None:
    """Routing uses explicit graph edges when available."""
    g = WarehouseGraph(default_speed_mps=2.2)
    g.add_edge(GraphEdge(from_node="FROM", to_node="STAGE-1", distance_meters=20.0, speed_mps=2.0))

    planner = MovementRoutePlanner(graph=g, solver_timeout_seconds=5)
    depot = _loc("DEPOT", x=0.0, y=0.0)
    t = _task(from_loc=_loc("FROM", x=10.0, y=0.0))
    result = planner.plan([t], ["FORKLIFT-1"], depot_location=depot, time_horizon_seconds=3600)

    assert "FAIL" not in result.solver_status and "INFEASIBLE" not in result.solver_status
    assert len(result.routes) >= 1


def test_routing_result_total_time_non_negative() -> None:
    planner = MovementRoutePlanner(solver_timeout_seconds=5)
    t = _task(from_loc=_loc("FROM", x=10.0, y=10.0))
    result = planner.plan([t], ["AGV-1"], time_horizon_seconds=3600)

    for route in result.routes:
        assert route.total_time_seconds >= 0.0
