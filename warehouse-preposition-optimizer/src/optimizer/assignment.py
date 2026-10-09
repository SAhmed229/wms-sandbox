"""OR-Tools CP-SAT staging assignment solver."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import structlog
from ortools.sat.python import cp_model

from src.models.inventory import Location, TemperatureZone
from src.models.movements import CandidateMovement, MovementTask

logger = structlog.get_logger(__name__)

# Scale factor for converting float scores to integers (CP-SAT requires integer coefficients).
_SCORE_SCALE = 1_000_000

# Hazmat classes that are mutually incompatible when occupying adjacent staging slots.
# Pairs are stored in sorted-tuple form to allow O(1) lookup.
_INCOMPATIBLE_HAZMAT_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("CLASS_3", "CLASS_5_1"),
        ("CLASS_3", "CLASS_5_2"),
        ("CLASS_1", "CLASS_2"),
        ("CLASS_1", "CLASS_3"),
        ("CLASS_1", "CLASS_4"),
        ("CLASS_1", "CLASS_5_1"),
        ("CLASS_1", "CLASS_5_2"),
        ("CLASS_1", "CLASS_6"),
        ("CLASS_1", "CLASS_7"),
        ("CLASS_1", "CLASS_8"),
        ("CLASS_1", "CLASS_9"),
        ("CLASS_7", "CLASS_1"),
        ("CLASS_7", "CLASS_2"),
        ("CLASS_7", "CLASS_3"),
        ("CLASS_7", "CLASS_4"),
        ("CLASS_7", "CLASS_5_1"),
        ("CLASS_7", "CLASS_5_2"),
        ("CLASS_7", "CLASS_6"),
        ("CLASS_7", "CLASS_8"),
        ("CLASS_7", "CLASS_9"),
    }
)


@dataclass
class AssignmentResult:
    """Result from the CP-SAT assignment solver.

    Args:
        tasks: MovementTask instances produced by the solver.
        solver_status: CP-SAT solver status string (OPTIMAL, FEASIBLE, INFEASIBLE, UNKNOWN).
        objective_value: Scaled objective (sum of selected scores * _SCORE_SCALE).
        wall_seconds: Elapsed solver wall-clock time in seconds.
    """

    tasks: list[MovementTask] = field(default_factory=list)
    solver_status: str = "UNKNOWN"
    objective_value: float = 0.0
    wall_seconds: float = 0.0


def _temperature_compatible(sku_zone: TemperatureZone, loc_zone: TemperatureZone) -> bool:
    """Return True if a SKU's temperature requirement is compatible with a location zone.

    Args:
        sku_zone: Temperature zone required by the SKU.
        loc_zone: Temperature zone of the target location.

    Returns:
        True when compatible (including CHILLED SKU in FROZEN zone).
    """
    if sku_zone == loc_zone:
        return True
    # CHILLED SKUs are acceptable in FROZEN zones.
    if sku_zone == TemperatureZone.CHILLED and loc_zone == TemperatureZone.FROZEN:
        return True
    return False


def _hazmat_incompatible(class_a: str | None, class_b: str | None) -> bool:
    """Return True if two hazmat classes must not occupy adjacent staging locations.

    Args:
        class_a: Hazmat class string for candidate A (e.g. 'CLASS_3'), or None.
        class_b: Hazmat class string for candidate B (e.g. 'CLASS_5_1'), or None.

    Returns:
        True when the pair is incompatible.
    """
    if class_a is None or class_b is None:
        return False
    pair = tuple(sorted([class_a, class_b]))
    return pair in _INCOMPATIBLE_HAZMAT_PAIRS  # type: ignore[operator]


def _manhattan(loc_a: Location, loc_b: Location) -> float:
    """Manhattan distance between two locations in meters.

    Args:
        loc_a: First location.
        loc_b: Second location.

    Returns:
        Distance in meters.
    """
    return abs(loc_a.x - loc_b.x) + abs(loc_a.y - loc_b.y)


class StagingAssignmentSolver:
    """CP-SAT binary assignment solver for pre-positioning candidates.

    Maximises ``sum(x[i][j] * score[i])`` subject to:
    - Each candidate is assigned to at most one staging location.
    - Each staging location receives at most one pallet.
    - Total assignments do not exceed ``available_resources``.
    - SKU temperature zone is compatible with the assigned location's zone.
    - No two incompatible hazmat SKUs are assigned to the same staging *area*
      (locations sharing the same aisle are considered adjacent).
    - Staging location distance from candidate's dock door does not exceed
      ``max_staging_distance_meters``.

    Args:
        solver_timeout_seconds: Maximum wall-clock seconds before the solver stops.
        max_staging_distance_meters: Hard upper bound on staging-to-source distance.
    """

    def __init__(
        self,
        solver_timeout_seconds: int = 10,
        max_staging_distance_meters: float = 50.0,
    ) -> None:
        self._timeout = solver_timeout_seconds
        self._max_distance = max_staging_distance_meters

    def solve(
        self,
        candidates: list[CandidateMovement],
        staging_locations: list[Location],
        available_resources: int,
        time_horizon_minutes: int = 120,
    ) -> AssignmentResult:
        """Run the CP-SAT solver and return an AssignmentResult.

        Args:
            candidates: Scored candidate movements to assign.
            staging_locations: Available staging locations to assign candidates to.
            available_resources: Maximum number of concurrent movements to dispatch.
            time_horizon_minutes: Unused in the current model; reserved for future
                time-window constraints.

        Returns:
            AssignmentResult with selected MovementTask list and solver metadata.
        """
        if not candidates or not staging_locations:
            logger.info(
                "assignment.empty_input",
                candidates=len(candidates),
                staging_locations=len(staging_locations),
            )
            return AssignmentResult(solver_status="INFEASIBLE")

        # Pre-filter: build feasibility mask — only (i, j) pairs that satisfy
        # temperature and distance constraints can be assigned.
        feasible: dict[tuple[int, int], bool] = {}
        for i, cand in enumerate(candidates):
            sku_zone = cand.from_location.temperature_zone
            hazmat = cand.from_location.temperature_zone  # placeholder; extracted below
            for j, loc in enumerate(staging_locations):
                loc_zone = loc.temperature_zone
                temp_ok = _temperature_compatible(sku_zone, loc_zone)
                dist_ok = _manhattan(cand.from_location, loc) <= self._max_distance
                feasible[(i, j)] = temp_ok and dist_ok

        model = cp_model.CpModel()

        # Decision variables: x[i][j] = 1 iff candidate i is assigned to location j.
        x: dict[tuple[int, int], cp_model.IntVar] = {}
        for i in range(len(candidates)):
            for j in range(len(staging_locations)):
                if feasible.get((i, j), False):
                    x[(i, j)] = model.new_bool_var(f"x_{i}_{j}")

        if not x:
            logger.warning("assignment.no_feasible_pairs")
            return AssignmentResult(solver_status="INFEASIBLE")

        # Constraint 1: Each candidate is assigned to at most one location.
        for i in range(len(candidates)):
            vars_for_i = [x[(i, j)] for j in range(len(staging_locations)) if (i, j) in x]
            if vars_for_i:
                model.add(sum(vars_for_i) <= 1)

        # Constraint 2: Each staging location receives at most one pallet.
        for j in range(len(staging_locations)):
            vars_for_j = [x[(i, j)] for i in range(len(candidates)) if (i, j) in x]
            if vars_for_j:
                model.add(sum(vars_for_j) <= 1)

        # Constraint 3: Total assignments ≤ available_resources.
        all_vars = list(x.values())
        model.add(sum(all_vars) <= available_resources)

        # Constraint 4: Hazmat adjacency — candidates in the same aisle must not be
        # incompatible hazmat classes.
        _add_hazmat_constraints(model, x, candidates, staging_locations)

        # Objective: maximise sum(x[i][j] * score[i]) scaled to integers.
        objective_terms: list[cp_model.LinearExprT] = []
        for (i, j), var in x.items():
            scaled_score = int(candidates[i].score * _SCORE_SCALE)
            objective_terms.append(var * scaled_score)

        model.maximize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(self._timeout)
        solver.parameters.log_search_progress = False

        status = solver.solve(model)
        status_name = solver.status_name(status)

        logger.info(
            "assignment.solved",
            status=status_name,
            wall_seconds=round(solver.wall_time, 3),
            objective=solver.objective_value,
            candidates=len(candidates),
            locations=len(staging_locations),
        )

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return AssignmentResult(
                solver_status=status_name,
                wall_seconds=solver.wall_time,
            )

        tasks: list[MovementTask] = []
        from datetime import UTC, datetime

        for (i, j), var in x.items():
            if solver.value(var) == 1:
                cand = candidates[i]
                loc = staging_locations[j]
                task = MovementTask(
                    movement_id=cand.movement_id,
                    sku_id=cand.sku_id,
                    from_location=cand.from_location,
                    to_location=loc,
                    score=cand.score,
                    score_components=cand.score_components,
                    reason=cand.reason + f" [OR-assigned to {loc.location_id}]",
                    estimated_duration_seconds=cand.estimated_duration_seconds,
                    assigned_resource="UNASSIGNED",
                    dispatched_at=datetime.now(UTC),
                )
                tasks.append(task)

        # Return in descending score order so callers can take top-N easily.
        tasks.sort(key=lambda t: t.score, reverse=True)

        return AssignmentResult(
            tasks=tasks,
            solver_status=status_name,
            objective_value=solver.objective_value / _SCORE_SCALE,
            wall_seconds=solver.wall_time,
        )


def _add_hazmat_constraints(
    model: cp_model.CpModel,
    x: dict[tuple[int, int], cp_model.IntVar],
    candidates: list[CandidateMovement],
    staging_locations: list[Location],
) -> None:
    """Add hazmat adjacency constraints to the model.

    Two candidates with incompatible hazmat classes must not be assigned to
    staging locations in the same aisle (aisle-level adjacency approximation).

    Args:
        model: CP-SAT model to add constraints to.
        x: Decision variable mapping.
        candidates: All candidate movements.
        staging_locations: All staging locations.
    """
    # Group staging location indices by aisle.
    aisle_to_locs: dict[str, list[int]] = {}
    for j, loc in enumerate(staging_locations):
        aisle_to_locs.setdefault(loc.aisle, []).append(j)

    for i_a in range(len(candidates)):
        for i_b in range(i_a + 1, len(candidates)):
            cand_a = candidates[i_a]
            cand_b = candidates[i_b]

            hazmat_a = cand_a.from_location.temperature_zone  # used as proxy placeholder
            # Extract actual hazmat class strings from SKU — the CandidateMovement only
            # carries from_location/to_location, so we reach into score_components for
            # a "hazmat_class" key when present. If absent, no constraint is added.
            haz_a = cand_a.score_components.get("hazmat_class")
            haz_b = cand_b.score_components.get("hazmat_class")

            if not _hazmat_incompatible(
                str(haz_a) if haz_a is not None else None,
                str(haz_b) if haz_b is not None else None,
            ):
                continue

            # Candidates are incompatible — they must not share an aisle.
            for aisle_locs in aisle_to_locs.values():
                vars_a = [x[(i_a, j)] for j in aisle_locs if (i_a, j) in x]
                vars_b = [x[(i_b, j)] for j in aisle_locs if (i_b, j) in x]
                if vars_a and vars_b:
                    # At most one of the two candidates may occupy this aisle.
                    model.add(sum(vars_a) + sum(vars_b) <= 1)
