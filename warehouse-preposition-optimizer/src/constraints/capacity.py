"""Capacity constraint: target location must have available weight and volume."""

from __future__ import annotations

from src.constraints.feasibility import ConstraintFilter
from src.ingestion.wms_adapter import WarehouseState
from src.models.constraints import ConstraintSeverity, ConstraintViolation, FeasibilityResult
from src.models.movements import CandidateMovement

_MAX_UTILIZATION_THRESHOLD = 0.95


class CapacityConstraint(ConstraintFilter):
    """HARD constraint: target location must have sufficient capacity.

    Uses location_utilization from warehouse state. If the location's
    current utilization exceeds the threshold, the movement is rejected.

    Args:
        max_utilization: Maximum allowed utilization fraction. Default 0.95.
    """

    def __init__(self, max_utilization: float = _MAX_UTILIZATION_THRESHOLD) -> None:
        self._max_utilization = max_utilization

    def check(
        self, movement: CandidateMovement, state: WarehouseState
    ) -> FeasibilityResult:
        """Check whether the target location has capacity for this movement.

        Args:
            movement: The candidate movement to evaluate.
            state: Current warehouse state with location_utilization map.

        Returns:
            FeasibilityResult indicating capacity availability.
        """
        target_id = movement.to_location.location_id
        utilization = state.location_utilization.get(target_id, 0.0)

        if utilization > self._max_utilization:
            return FeasibilityResult(
                feasible=False,
                violations=[
                    ConstraintViolation(
                        constraint_type="capacity",
                        description=(
                            f"Location {target_id} is at {utilization:.1%} utilization "
                            f"(max allowed: {self._max_utilization:.1%})."
                        ),
                        severity=ConstraintSeverity.HARD,
                    )
                ],
            )

        return FeasibilityResult(feasible=True, violations=[])
