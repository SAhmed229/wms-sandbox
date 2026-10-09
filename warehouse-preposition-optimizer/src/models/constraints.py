"""Constraint violation and feasibility result models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class ConstraintSeverity(str, Enum):
    """Severity level of a constraint violation."""

    HARD = "HARD"
    SOFT = "SOFT"


class ConstraintViolation(BaseModel):
    """A single constraint violation with type and description.

    Args:
        constraint_type: Identifier for the constraint (e.g. 'temperature', 'hazmat').
        description: Human-readable explanation of the violation.
        severity: Whether this is a hard (blocking) or soft (advisory) violation.
    """

    model_config = ConfigDict(from_attributes=True)

    constraint_type: str
    description: str
    severity: ConstraintSeverity


class FeasibilityResult(BaseModel):
    """Result of running a movement through the constraint engine.

    Args:
        feasible: True if no hard violations were found.
        violations: List of all violations (hard and soft) encountered.
    """

    model_config = ConfigDict(from_attributes=True)

    feasible: bool
    violations: list[ConstraintViolation] = []
