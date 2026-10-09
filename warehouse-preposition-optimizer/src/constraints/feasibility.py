"""Feasibility engine: runs all constraint filters against a movement candidate."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.ingestion.wms_adapter import WarehouseState
from src.models.constraints import ConstraintSeverity, ConstraintViolation, FeasibilityResult
from src.models.movements import CandidateMovement


class ConstraintFilter(ABC):
    """Abstract base class for movement constraint filters.

    Each filter checks one type of constraint (temperature, hazmat, capacity).
    Hard constraint violations immediately make the movement infeasible.
    """

    @abstractmethod
    def check(
        self, movement: CandidateMovement, state: WarehouseState
    ) -> FeasibilityResult:
        """Check if a movement satisfies this constraint.

        Args:
            movement: The candidate movement to evaluate.
            state: Current warehouse state snapshot.

        Returns:
            FeasibilityResult indicating pass or violation details.
        """


class FeasibilityEngine:
    """Runs a pipeline of constraint filters against candidate movements.

    Hard violations stop evaluation immediately. Soft violations are
    collected but do not prevent the movement from being scored.

    Args:
        filters: Ordered list of ConstraintFilter instances to apply.
    """

    def __init__(self, filters: list[ConstraintFilter]) -> None:
        self._filters = filters

    def evaluate(
        self, movement: CandidateMovement, state: WarehouseState
    ) -> FeasibilityResult:
        """Run all filters and return aggregated feasibility result.

        Stops on the first HARD violation. Collects all SOFT violations.

        Args:
            movement: The candidate movement to evaluate.
            state: Current warehouse state snapshot.

        Returns:
            FeasibilityResult with feasible=True only if no hard violations found.
        """
        all_violations: list[ConstraintViolation] = []

        for constraint_filter in self._filters:
            result = constraint_filter.check(movement, state)
            for violation in result.violations:
                if violation.severity == ConstraintSeverity.HARD:
                    # Hard violation: stop immediately, movement is infeasible
                    return FeasibilityResult(
                        feasible=False,
                        violations=all_violations + [violation],
                    )
                all_violations.append(violation)

        return FeasibilityResult(feasible=True, violations=all_violations)
