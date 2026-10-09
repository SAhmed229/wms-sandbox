"""Movement candidate and task domain models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from src.models.inventory import Location


class MovementStatus(str, Enum):
    """Lifecycle status of a movement task."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CandidateMovement(BaseModel):
    """A scored candidate for pre-positioning movement.

    Args:
        movement_id: Unique identifier for this movement candidate.
        sku_id: SKU to be moved.
        from_location: Current location of the SKU.
        to_location: Target staging location.
        score: Computed value function score V(m).
        score_components: Breakdown of each scoring term.
        reason: Human-readable reason for this movement suggestion.
        estimated_duration_seconds: Estimated time to complete movement.
    """

    model_config = ConfigDict(from_attributes=True)

    movement_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    sku_id: str
    from_location: Location
    to_location: Location
    score: float = 0.0
    score_components: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    estimated_duration_seconds: int = 0


class MovementTask(CandidateMovement):
    """A dispatched movement task assigned to a resource.

    Args:
        assigned_resource: Resource ID (AGV, forklift) assigned.
        status: Current task status.
        dispatched_at: Time the task was dispatched.
        completed_at: Time the task was completed, if applicable.
    """

    assigned_resource: str
    status: MovementStatus = MovementStatus.PENDING
    dispatched_at: datetime
    completed_at: datetime | None = None
