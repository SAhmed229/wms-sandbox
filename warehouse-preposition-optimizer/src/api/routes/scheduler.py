"""Scheduler control and status endpoints.

Provides:
* ``POST /scheduler/trigger`` — run an immediate scheduling cycle.
* ``GET  /scheduler/status``  — return cycle statistics.

The trigger endpoint is the recommended way to react to external warehouse
events (new appointment created, dock door opened, shift start) without
waiting for the next timed cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────


class TriggerResponse(BaseModel):
    """Response from a manual scheduler trigger.

    Args:
        candidates_scored: Number of movement candidates generated and scored.
        tasks_dispatched: Number of tasks pushed to the queue.
        reason: Free-text reason for the trigger.
        triggered_at: UTC timestamp when the cycle was triggered.
    """

    candidates_scored: int
    tasks_dispatched: int
    reason: str
    triggered_at: datetime


class SchedulerStatusResponse(BaseModel):
    """Current scheduler operating statistics.

    Args:
        cycle_count: Total cycles run since startup.
        last_cycle_at: UTC timestamp of the last completed cycle (None if never run).
        last_candidates_scored: Candidates generated in the last cycle.
        last_tasks_dispatched: Tasks dispatched in the last cycle.
        avg_cycle_duration_seconds: Rolling average cycle wall-clock time.
        is_running: True while the background scheduler loop is active.
    """

    cycle_count: int
    last_cycle_at: datetime | None
    last_candidates_scored: int
    last_tasks_dispatched: int
    avg_cycle_duration_seconds: float
    is_running: bool


# ─────────────────────────────────────────────────────────────────────────────
# Dependencies
# ─────────────────────────────────────────────────────────────────────────────


def _get_scheduler(request: Request) -> Any:
    return request.app.state.scheduler


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_cycle(
    request: Request,
    scheduler: Annotated[Any, Depends(_get_scheduler)],
    reason: str = "manual",
) -> TriggerResponse:
    """Trigger an immediate scheduling cycle outside the timed loop.

    Args:
        reason: Free-text reason (logged and broadcast via WebSocket).

    Returns:
        :class:`TriggerResponse` with candidate and dispatch counts.
    """
    from datetime import UTC

    now = datetime.now(UTC)
    candidates, tasks = await scheduler.run_cycle()

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast("cycle_complete", {
            "candidates_scored": len(candidates),
            "tasks_dispatched": len(tasks),
            "reason": reason,
        })

    logger.info(
        "scheduler.triggered",
        reason=reason,
        candidates=len(candidates),
        dispatched=len(tasks),
    )
    return TriggerResponse(
        candidates_scored=len(candidates),
        tasks_dispatched=len(tasks),
        reason=reason,
        triggered_at=now,
    )


@router.get("/status", response_model=SchedulerStatusResponse)
async def get_status(
    scheduler: Annotated[Any, Depends(_get_scheduler)],
    request: Request,
) -> SchedulerStatusResponse:
    """Return current scheduler cycle statistics.

    Returns:
        :class:`SchedulerStatusResponse` with cycle count, timing, and last results.
    """
    stats = scheduler.get_status()
    loop_task = getattr(request.app.state, "scheduler_loop_task", None)
    stats["is_running"] = loop_task is not None and not loop_task.done()
    return SchedulerStatusResponse(**stats)
