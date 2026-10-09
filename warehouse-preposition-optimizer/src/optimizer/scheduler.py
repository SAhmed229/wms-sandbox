"""Pre-positioning scheduler: full pipeline from WMS state to dispatched tasks."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from src.constraints.feasibility import FeasibilityEngine
from src.monitoring.metrics import (
    AVG_SCORE,
    CONSTRAINT_VIOLATIONS,
    MOVEMENTS_DISPATCHED,
    MOVEMENTS_SCORED,
)
from src.dispatch.task_queue import TaskQueue
from src.ingestion.wms_adapter import WMSAdapter, WarehouseState
from src.models.inventory import InventoryPosition, Location
from src.models.movements import CandidateMovement, MovementTask
from src.scoring.value_function import MovementScorer, ScoringContext

if TYPE_CHECKING:
    from src.dispatch.rejection_store import RejectionStore
    from src.optimizer.assignment import StagingAssignmentSolver
    from src.optimizer.rl_policy import RLPolicyInference

logger = structlog.get_logger(__name__)

_CYCLE_DURATION_WINDOW = 20  # rolling average window size


@dataclass
class SchedulerConfig:
    """Configuration for the pre-positioning scheduler.

    Args:
        cycle_interval_seconds: Time between scheduling cycles.
        dispatch_batch_size: Number of tasks to dispatch per cycle.
        horizon_hours: Planning horizon for order lookups.
        max_candidates: Maximum candidates to evaluate per cycle.
        min_score_threshold: Minimum score to include a candidate.
        use_or_optimization: Enable Phase 3 OR-Tools assignment solver.
        available_resources: Resource budget passed to the assignment solver.
        solver_timeout_seconds: CP-SAT solver wall-clock time limit.
        max_staging_distance_meters: Maximum staging-to-source distance for assignment.
    """

    cycle_interval_seconds: int = 60
    dispatch_batch_size: int = 5
    horizon_hours: float = 24.0
    max_candidates: int = 50
    min_score_threshold: float = 0.1
    use_or_optimization: bool = False
    available_resources: int = 5
    solver_timeout_seconds: int = 10
    max_staging_distance_meters: float = 50.0
    use_rl_policy: bool = False
    rl_policy_path: str = "models/policy.onnx"


def _manhattan_distance(loc_a: Location, loc_b: Location) -> float:
    """Compute Manhattan distance between two locations.

    Args:
        loc_a: First location.
        loc_b: Second location.

    Returns:
        Manhattan distance in meters.
    """
    return abs(loc_a.x - loc_b.x) + abs(loc_a.y - loc_b.y)


def _find_best_staging_location(
    position: InventoryPosition,
    dock_door: int,
    staging_locations: list[Location],
) -> Location | None:
    """Find the best staging location for a position relative to a dock door.

    Prefers staging locations assigned to the target dock door. Falls back
    to the closest staging location by Manhattan distance.

    Args:
        position: The inventory position to be moved.
        dock_door: The dock door number for the target appointment.
        staging_locations: All available staging locations.

    Returns:
        Best staging location, or None if no staging locations available.
    """
    if not staging_locations:
        return None

    # First: staging locations assigned to this dock door
    door_locations = [
        loc for loc in staging_locations if loc.nearest_dock_door == dock_door
    ]

    if door_locations:
        return min(door_locations, key=lambda loc: _manhattan_distance(position.location, loc))

    # Fallback: closest staging location overall
    return min(staging_locations, key=lambda loc: _manhattan_distance(position.location, loc))


class PrePositionScheduler:
    """Runs the full pre-positioning pipeline: fetch → generate → filter → score → dispatch.

    Args:
        scorer: Movement scoring engine.
        feasibility: Constraint feasibility engine.
        wms: WMS adapter for state retrieval.
        task_queue: Redis-backed task queue for dispatching.
        config: Scheduler configuration parameters.
        rl_policy: Optional Phase 4 RL policy inference engine. When provided and
            ``config.use_rl_policy`` is True, the RL policy selects movements with
            OR-Tools as the fallback.
        rejection_store: Optional Phase 5 rejection store.  When provided, SKUs
            with active rejections are filtered out before dispatch.
    """

    def __init__(
        self,
        scorer: MovementScorer,
        feasibility: FeasibilityEngine,
        wms: WMSAdapter,
        task_queue: TaskQueue,
        config: SchedulerConfig,
        rl_policy: RLPolicyInference | None = None,
        rejection_store: RejectionStore | None = None,
    ) -> None:
        self._scorer = scorer
        self._feasibility = feasibility
        self._wms = wms
        self._task_queue = task_queue
        self._config = config
        self._rl_policy = rl_policy
        self._rejection_store = rejection_store
        # Cycle statistics
        self._cycle_count: int = 0
        self._last_cycle_at: datetime | None = None
        self._last_candidates: int = 0
        self._last_dispatched: int = 0
        self._cycle_durations: deque[float] = deque(maxlen=_CYCLE_DURATION_WINDOW)

    async def generate_candidates(self) -> list[CandidateMovement]:
        """Run the full candidate generation pipeline.

        Steps:
        1. Fetch warehouse state from WMS.
        2. Generate (inventory_position, staging_location) pairs per appointment.
        3. Filter infeasible movements via constraint engine.
        4. Score each feasible candidate.
        5. Deduplicate (keep highest score per SKU).
        6. Return top N by score.

        Returns:
            Scored, feasible, deduplicated candidates sorted by score descending.
        """
        state = await self._wms.get_warehouse_state(self._config.horizon_hours)

        if not state.appointments:
            logger.info("scheduler.no_appointments")
            return []

        # Build inventory lookup for ML feature quality (Phase 2).
        # Most-recent position per SKU (first wins if duplicates exist).
        inventory_by_sku = {
            pos.sku.sku_id: pos
            for pos in reversed(state.inventory_positions)
        }

        context = ScoringContext(
            orders=state.outbound_orders,
            appointments=state.appointments,
            resource_utilization=_compute_avg_utilization(state.resource_utilization),
            inventory_by_sku=inventory_by_sku,
        )

        candidates: list[CandidateMovement] = []

        for appointment in state.appointments:
            for position in state.inventory_positions:
                staging_loc = _find_best_staging_location(
                    position, appointment.dock_door, state.staging_locations
                )
                if staging_loc is None:
                    continue

                # Skip if already at a staging location for this door
                if (
                    position.location.is_staging
                    and position.location.nearest_dock_door == appointment.dock_door
                ):
                    continue

                # Skip if source and target are the same
                if position.location.location_id == staging_loc.location_id:
                    continue

                candidate = CandidateMovement(
                    sku_id=position.sku.sku_id,
                    from_location=position.location,
                    to_location=staging_loc,
                    reason=(
                        f"Pre-stage for appointment {appointment.appointment_id} "
                        f"at dock door {appointment.dock_door}"
                    ),
                )

                # Check feasibility first (hard constraints)
                feasibility_result = self._feasibility.evaluate(candidate, state)
                if not feasibility_result.feasible:
                    for v in feasibility_result.violations:
                        CONSTRAINT_VIOLATIONS.labels(
                            constraint_type=v.constraint_type
                        ).inc()
                    logger.debug(
                        "scheduler.candidate_infeasible",
                        sku_id=candidate.sku_id,
                        violations=[v.description for v in feasibility_result.violations],
                    )
                    continue

                # Score the candidate
                score = self._scorer.score(candidate, context)
                MOVEMENTS_SCORED.inc()
                if score < self._config.min_score_threshold:
                    continue

                candidates.append(candidate)

        # Deduplicate: keep highest score per SKU
        best_by_sku: dict[str, CandidateMovement] = {}
        for candidate in candidates:
            existing = best_by_sku.get(candidate.sku_id)
            if existing is None or candidate.score > existing.score:
                best_by_sku[candidate.sku_id] = candidate

        deduped = sorted(best_by_sku.values(), key=lambda c: c.score, reverse=True)

        # Phase 5: filter out SKUs currently suppressed by the rejection store
        if self._rejection_store is not None:
            filtered: list[CandidateMovement] = []
            for c in deduped:
                if await self._rejection_store.is_sku_suppressed(c.sku_id):
                    logger.debug("scheduler.candidate_rejected_suppressed", sku_id=c.sku_id)
                else:
                    filtered.append(c)
            deduped = filtered

        top_n = deduped[: self._config.max_candidates]

        if top_n:
            AVG_SCORE.set(sum(c.score for c in top_n) / len(top_n))

        logger.info(
            "scheduler.candidates_generated",
            count=len(top_n),
            total_evaluated=len(candidates),
        )
        return top_n

    async def dispatch_top_movements(self, n: int = 5) -> list[MovementTask]:
        """Generate candidates and dispatch the top N as tasks.

        Args:
            n: Number of top candidates to dispatch.

        Returns:
            List of dispatched MovementTask instances.
        """
        candidates = await self.generate_candidates()
        top = candidates[:n]
        tasks: list[MovementTask] = []

        for candidate in top:
            task = MovementTask(
                movement_id=candidate.movement_id,
                sku_id=candidate.sku_id,
                from_location=candidate.from_location,
                to_location=candidate.to_location,
                score=candidate.score,
                score_components=candidate.score_components,
                reason=candidate.reason,
                estimated_duration_seconds=candidate.estimated_duration_seconds,
                assigned_resource="UNASSIGNED",
                dispatched_at=datetime.now(UTC),
            )
            await self._task_queue.push(task)
            tasks.append(task)
            MOVEMENTS_DISPATCHED.inc()
            logger.info(
                "scheduler.task_dispatched",
                movement_id=str(task.movement_id),
                sku_id=task.sku_id,
                score=round(task.score, 4),
            )

        return tasks

    async def run_cycle(
        self,
    ) -> tuple[list[CandidateMovement], list[MovementTask]]:
        """Run one full scheduling cycle.

        Dispatch path selection (in priority order):
        1. Phase 4 RL policy (``config.use_rl_policy`` + ``rl_policy`` provided):
           ONNX policy selects a single candidate; falls back to OR-Tools internally
           when the policy selects NO_OP or fails.
        2. Phase 3 OR-Tools (``config.use_or_optimization``): CP-SAT assignment solver.
        3. Phase 1/2 greedy: top-N by score.

        Returns:
            Tuple of (all_candidates, dispatched_tasks).
        """
        logger.info("scheduler.cycle_start")
        _t0 = time.monotonic()
        candidates = await self.generate_candidates()
        tasks: list[MovementTask] = []

        if self._config.use_rl_policy and self._rl_policy is not None and candidates:
            tasks = await self._run_rl_cycle(candidates)
        elif self._config.use_or_optimization and candidates:
            tasks = await self._run_or_cycle(candidates)
        else:
            top = candidates[: self._config.dispatch_batch_size]
            for candidate in top:
                task = MovementTask(
                    movement_id=candidate.movement_id,
                    sku_id=candidate.sku_id,
                    from_location=candidate.from_location,
                    to_location=candidate.to_location,
                    score=candidate.score,
                    score_components=candidate.score_components,
                    reason=candidate.reason,
                    estimated_duration_seconds=candidate.estimated_duration_seconds,
                    assigned_resource="UNASSIGNED",
                    dispatched_at=datetime.now(UTC),
                )
                await self._task_queue.push(task)
                tasks.append(task)
                MOVEMENTS_DISPATCHED.inc()

        self._cycle_durations.append(time.monotonic() - _t0)
        self._cycle_count += 1
        self._last_cycle_at = datetime.now(UTC)
        self._last_candidates = len(candidates)
        self._last_dispatched = len(tasks)

        logger.info(
            "scheduler.cycle_complete",
            candidates=len(candidates),
            dispatched=len(tasks),
        )
        return candidates, tasks

    def get_status(self) -> dict[str, Any]:
        """Return current cycle statistics as a plain dict.

        Returns:
            Dict compatible with :class:`~src.api.routes.scheduler.SchedulerStatusResponse`.
        """
        avg_duration = (
            sum(self._cycle_durations) / len(self._cycle_durations)
            if self._cycle_durations else 0.0
        )
        return {
            "cycle_count": self._cycle_count,
            "last_cycle_at": self._last_cycle_at,
            "last_candidates_scored": self._last_candidates,
            "last_tasks_dispatched": self._last_dispatched,
            "avg_cycle_duration_seconds": round(avg_duration, 3),
            "is_running": False,  # overridden by the route handler
        }

    async def _run_or_cycle(
        self, candidates: list[CandidateMovement]
    ) -> list[MovementTask]:
        """Use CP-SAT assignment solver to select and dispatch tasks.

        Imports the solver lazily so the module still loads when OR-Tools is not
        installed (the flag guards execution, not import time).

        Args:
            candidates: Scored, feasible candidates from generate_candidates().

        Returns:
            Dispatched MovementTask list.
        """
        from src.optimizer.assignment import StagingAssignmentSolver  # noqa: PLC0415

        # Collect all unique staging locations referenced by the candidates.
        staging_locs: list[Location] = list(
            {c.to_location.location_id: c.to_location for c in candidates}.values()
        )

        solver = StagingAssignmentSolver(
            solver_timeout_seconds=self._config.solver_timeout_seconds,
            max_staging_distance_meters=self._config.max_staging_distance_meters,
        )
        result = solver.solve(
            candidates=candidates,
            staging_locations=staging_locs,
            available_resources=self._config.available_resources,
        )

        logger.info(
            "scheduler.or_assignment_complete",
            status=result.solver_status,
            selected=len(result.tasks),
            objective=round(result.objective_value, 4),
        )

        tasks: list[MovementTask] = []
        for task in result.tasks[: self._config.dispatch_batch_size]:
            await self._task_queue.push(task)
            tasks.append(task)
            MOVEMENTS_DISPATCHED.inc()
            logger.info(
                "scheduler.task_dispatched",
                movement_id=str(task.movement_id),
                sku_id=task.sku_id,
                score=round(task.score, 4),
                via="or_tools",
            )
        return tasks

    async def _run_rl_cycle(
        self, candidates: list[CandidateMovement]
    ) -> list[MovementTask]:
        """Use RL policy (with OR-Tools fallback) to select and dispatch tasks.

        Builds a minimal observation vector from candidate score components and
        delegates action selection to RLPolicyInference.

        Args:
            candidates: Scored, feasible candidates from generate_candidates().

        Returns:
            Dispatched MovementTask list.
        """
        import numpy as np  # noqa: PLC0415

        assert self._rl_policy is not None

        # Build a flat observation from candidate features (simplified — a real
        # deployment would use the full Gymnasium observation from the digital twin).
        max_cands = 20
        obs_size = max_cands * 6 + 10 * 4 + 4 * 3 + 3
        obs = np.zeros(obs_size, dtype=np.float32)
        for i, cand in enumerate(candidates[:max_cands]):
            sc = cand.score_components
            base = i * 6
            obs[base:base + 6] = [
                cand.score,
                sc.get("t_saved", 0.0),
                sc.get("p_load", 0.0),
                sc.get("w_order", 0.0),
                sc.get("c_move", 0.0),
                sc.get("c_opportunity", 0.0),
            ]

        staging_locs: list[Location] = list(
            {c.to_location.location_id: c.to_location for c in candidates}.values()
        )

        selected_tasks = self._rl_policy.select(obs, candidates, staging_locs)

        tasks: list[MovementTask] = []
        for task in selected_tasks[: self._config.dispatch_batch_size]:
            await self._task_queue.push(task)
            tasks.append(task)
            MOVEMENTS_DISPATCHED.inc()
            logger.info(
                "scheduler.task_dispatched",
                movement_id=str(task.movement_id),
                sku_id=task.sku_id,
                score=round(task.score, 4),
                via="rl_policy",
            )
        return tasks


def _compute_avg_utilization(resource_utilization: dict[str, float]) -> float:
    """Compute average resource utilization across the fleet.

    Args:
        resource_utilization: Map of resource_id to utilization fraction.

    Returns:
        Average utilization, or 0.0 if no resources reported.
    """
    if not resource_utilization:
        return 0.0
    return sum(resource_utilization.values()) / len(resource_utilization)
