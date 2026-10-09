"""Movement candidate and task management endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from src.dispatch.rejection_store import RejectionRecord
from src.models.movements import CandidateMovement, MovementStatus, MovementTask

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/movements", tags=["movements"])


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────


class RejectionResponse(BaseModel):
    """Confirmation of a rejection being recorded.

    Args:
        status: Always ``"rejected"``.
        movement_id: UUID of the rejected candidate.
        sku_id: SKU that was in the candidate.
        reason: Human-readable rejection reason.
        suppressed_until_seconds: How long the SKU is suppressed from re-scoring.
    """

    status: str
    movement_id: str
    sku_id: str
    reason: str
    suppressed_for_seconds: int


class RejectionHistoryItem(BaseModel):
    """A single entry in the rejection history.

    Args:
        movement_id: UUID of the rejected movement.
        sku_id: SKU in the rejected movement.
        reason: Operator-supplied rejection reason.
        rejected_at: UTC timestamp of the rejection.
        ttl_seconds: Original suppression window in seconds.
    """

    movement_id: str
    sku_id: str
    reason: str
    rejected_at: datetime
    ttl_seconds: int


# ─────────────────────────────────────────────────────────────────────────────
# Dependencies
# ─────────────────────────────────────────────────────────────────────────────


def _get_scheduler(request: Request) -> Any:
    return request.app.state.scheduler


def _get_task_queue(request: Request) -> Any:
    return request.app.state.task_queue


def _get_rejection_store(request: Request) -> Any:
    return getattr(request.app.state, "rejection_store", None)


def _get_ws_manager(request: Request) -> Any:
    return getattr(request.app.state, "ws_manager", None)


# ─────────────────────────────────────────────────────────────────────────────
# Candidates
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/candidates", response_model=list[CandidateMovement])
async def get_candidates(
    scheduler: Annotated[Any, Depends(_get_scheduler)],
) -> list[CandidateMovement]:
    """Generate and return scored movement candidates.

    Returns:
        List of :class:`CandidateMovement` instances sorted by score descending.
    """
    return await scheduler.generate_candidates()


@router.post("/{movement_id}/approve", response_model=MovementTask)
async def approve_movement(
    movement_id: UUID,
    scheduler: Annotated[Any, Depends(_get_scheduler)],
    task_queue: Annotated[Any, Depends(_get_task_queue)],
    ws_manager: Annotated[Any, Depends(_get_ws_manager)],
) -> MovementTask:
    """Approve a candidate movement and dispatch it as a task.

    Args:
        movement_id: UUID of the candidate movement to approve.

    Returns:
        The dispatched :class:`MovementTask`.
    """
    candidates = await scheduler.generate_candidates()
    candidate = next((c for c in candidates if c.movement_id == movement_id), None)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Candidate {movement_id} not found.")

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
    await task_queue.push(task)

    if ws_manager is not None:
        await ws_manager.broadcast("task_dispatched", {
            "movement_id": str(task.movement_id),
            "sku_id": task.sku_id,
            "score": round(task.score, 4),
            "from_location_id": task.from_location.location_id,
            "to_location_id": task.to_location.location_id,
        })

    logger.info("movement.approved", movement_id=str(movement_id))
    return task


@router.post("/{movement_id}/reject", response_model=RejectionResponse)
async def reject_movement(
    movement_id: UUID,
    request: Request,
    reason: Annotated[str, Body(embed=True)],
    rejection_store: Annotated[Any, Depends(_get_rejection_store)],
    ws_manager: Annotated[Any, Depends(_get_ws_manager)],
) -> RejectionResponse:
    """Record an operator rejection for a candidate movement.

    The candidate's SKU is suppressed from the scheduler for the default
    rejection TTL (1 hour) so it does not immediately re-appear.

    Args:
        movement_id: UUID of the candidate to reject.
        reason: Human-readable rejection reason.

    Returns:
        :class:`RejectionResponse` confirming the rejection.
    """
    # Resolve sku_id: best-effort from active candidates
    sku_id = ""
    try:
        scheduler = request.app.state.scheduler
        candidates = await scheduler.generate_candidates()
        match = next((c for c in candidates if c.movement_id == movement_id), None)
        if match:
            sku_id = match.sku_id
    except Exception:
        pass

    ttl = 3600
    if rejection_store is not None:
        record: RejectionRecord = await rejection_store.record(
            movement_id=str(movement_id),
            sku_id=sku_id,
            reason=reason,
        )
        ttl = record.ttl_seconds

    if ws_manager is not None:
        await ws_manager.broadcast("movement_rejected", {
            "movement_id": str(movement_id),
            "sku_id": sku_id,
            "reason": reason,
            "ttl_seconds": ttl,
        })

    logger.info("movement.rejected", movement_id=str(movement_id), sku_id=sku_id, reason=reason)
    return RejectionResponse(
        status="rejected",
        movement_id=str(movement_id),
        sku_id=sku_id,
        reason=reason,
        suppressed_for_seconds=ttl,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Active tasks
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/active", response_model=list[MovementTask])
async def get_active_tasks(
    task_queue: Annotated[Any, Depends(_get_task_queue)],
) -> list[MovementTask]:
    """Return all currently active movement tasks.

    Returns:
        List of active :class:`MovementTask` instances.
    """
    return await task_queue.get_active_tasks()


@router.post("/{movement_id}/acknowledge", response_model=MovementTask)
async def acknowledge_movement(
    movement_id: UUID,
    task_queue: Annotated[Any, Depends(_get_task_queue)],
    ws_manager: Annotated[Any, Depends(_get_ws_manager)],
) -> MovementTask:
    """Acknowledge receipt of a task (PENDING → IN_PROGRESS).

    Called by the operator or AGV system when the task has been picked up
    and execution has started.

    Args:
        movement_id: UUID of the task to acknowledge.

    Returns:
        Updated :class:`MovementTask`.
    """
    task = await task_queue.get_task(str(movement_id))
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {movement_id} not found.")

    if task.status != MovementStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Task {movement_id} is in status {task.status.value!r}, not PENDING.",
        )

    await task_queue.update_status(str(movement_id), MovementStatus.IN_PROGRESS)

    if ws_manager is not None:
        await ws_manager.broadcast("task_status_changed", {
            "movement_id": str(movement_id),
            "sku_id": task.sku_id,
            "old_status": MovementStatus.PENDING.value,
            "new_status": MovementStatus.IN_PROGRESS.value,
        })

    task.status = MovementStatus.IN_PROGRESS
    logger.info("movement.acknowledged", movement_id=str(movement_id))
    return task


@router.post("/{movement_id}/complete", response_model=MovementTask)
async def complete_movement(
    movement_id: UUID,
    task_queue: Annotated[Any, Depends(_get_task_queue)],
    ws_manager: Annotated[Any, Depends(_get_ws_manager)],
) -> MovementTask:
    """Mark a task as completed (IN_PROGRESS → COMPLETED).

    Called by the operator or AGV system upon physical completion of the move.

    Args:
        movement_id: UUID of the task to complete.

    Returns:
        Updated :class:`MovementTask`.
    """
    task = await task_queue.get_task(str(movement_id))
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {movement_id} not found.")

    if task.status not in (MovementStatus.PENDING, MovementStatus.IN_PROGRESS):
        raise HTTPException(
            status_code=409,
            detail=f"Task {movement_id} is in status {task.status.value!r} and cannot be completed.",
        )

    old_status = task.status.value
    await task_queue.update_status(str(movement_id), MovementStatus.COMPLETED)

    if ws_manager is not None:
        await ws_manager.broadcast("task_status_changed", {
            "movement_id": str(movement_id),
            "sku_id": task.sku_id,
            "old_status": old_status,
            "new_status": MovementStatus.COMPLETED.value,
        })

    task.status = MovementStatus.COMPLETED
    task.completed_at = datetime.now(UTC)
    logger.info("movement.completed", movement_id=str(movement_id))
    return task


# ─────────────────────────────────────────────────────────────────────────────
# Rejection history
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/rejected", response_model=list[RejectionHistoryItem])
async def get_rejected_movements(
    rejection_store: Annotated[Any, Depends(_get_rejection_store)],
    limit: int = 50,
) -> list[RejectionHistoryItem]:
    """Return recent rejection history, newest first.

    Args:
        limit: Maximum number of records to return (default 50, max 200).

    Returns:
        List of :class:`RejectionHistoryItem` instances.
    """
    if rejection_store is None:
        return []
    limit = min(limit, 200)
    records = await rejection_store.get_history(limit=limit)
    return [
        RejectionHistoryItem(
            movement_id=r.movement_id,
            sku_id=r.sku_id,
            reason=r.reason,
            rejected_at=r.rejected_at,
            ttl_seconds=r.ttl_seconds,
        )
        for r in records
    ]


@router.delete("/{movement_id}/rejection")
async def clear_rejection(
    movement_id: UUID,
    rejection_store: Annotated[Any, Depends(_get_rejection_store)],
) -> dict[str, str]:
    """Lift an active rejection so the SKU can re-appear in scoring.

    Args:
        movement_id: UUID of the movement whose rejection to clear.

    Returns:
        Confirmation dict.
    """
    if rejection_store is None:
        raise HTTPException(status_code=503, detail="Rejection store unavailable.")
    cleared = await rejection_store.clear(str(movement_id))
    if not cleared:
        raise HTTPException(status_code=404, detail=f"No active rejection found for {movement_id}.")
    logger.info("movement.rejection_cleared", movement_id=str(movement_id))
    return {"status": "cleared", "movement_id": str(movement_id)}
