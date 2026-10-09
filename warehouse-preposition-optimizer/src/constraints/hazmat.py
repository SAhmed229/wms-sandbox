"""Hazmat compatibility constraint: incompatible classes cannot share a bay."""

from __future__ import annotations

from src.constraints.feasibility import ConstraintFilter
from src.ingestion.wms_adapter import WarehouseState
from src.models.constraints import ConstraintSeverity, ConstraintViolation, FeasibilityResult
from src.models.inventory import HazmatClass
from src.models.movements import CandidateMovement

# Simplified DOT incompatibility pairs (symmetric).
# CLASS_1 and CLASS_7 are incompatible with everything else.
_INCOMPATIBLE_PAIRS: frozenset[frozenset[HazmatClass]] = frozenset(
    {
        frozenset({HazmatClass.CLASS_3, HazmatClass.CLASS_5_1}),
        frozenset({HazmatClass.CLASS_3, HazmatClass.CLASS_5_2}),
        # CLASS_1 incompatible with all others represented as pairs
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_2}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_3}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_4}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_5_1}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_5_2}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_6}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_7}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_8}),
        frozenset({HazmatClass.CLASS_1, HazmatClass.CLASS_9}),
        # CLASS_7 incompatible with all others
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_2}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_3}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_4}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_5_1}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_5_2}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_6}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_8}),
        frozenset({HazmatClass.CLASS_7, HazmatClass.CLASS_9}),
    }
)


def _are_incompatible(class_a: HazmatClass, class_b: HazmatClass) -> bool:
    """Check if two hazmat classes are incompatible for co-location.

    Args:
        class_a: First hazmat class.
        class_b: Second hazmat class.

    Returns:
        True if the two classes cannot share a bay.
    """
    return frozenset({class_a, class_b}) in _INCOMPATIBLE_PAIRS


class HazmatConstraint(ConstraintFilter):
    """HARD constraint: incompatible hazmat classes cannot share a bay.

    Uses a simplified DOT incompatibility table. Non-hazmat SKUs always pass.
    Checks what hazmat material is already present at the target bay using
    the staging locations from warehouse state.
    """

    def check(
        self, movement: CandidateMovement, state: WarehouseState
    ) -> FeasibilityResult:
        """Check hazmat compatibility for the movement's target bay.

        Args:
            movement: The candidate movement to evaluate.
            state: Current warehouse state used to find existing hazmat in bay.

        Returns:
            FeasibilityResult indicating compatibility.
        """
        # Find the moving SKU's hazmat class
        moving_hazmat: HazmatClass | None = None
        for position in state.inventory_positions:
            if position.sku.sku_id == movement.sku_id:
                moving_hazmat = position.sku.hazmat_class
                break

        # Non-hazmat SKU: always passes hazmat check
        if moving_hazmat is None:
            return FeasibilityResult(feasible=True, violations=[])

        target_aisle = movement.to_location.aisle
        target_bay = movement.to_location.bay

        # Collect hazmat classes already present in the target bay
        existing_hazmat_classes: list[HazmatClass] = []
        for position in state.inventory_positions:
            loc = position.location
            if (
                loc.aisle == target_aisle
                and loc.bay == target_bay
                and loc.location_id != movement.from_location.location_id
                and position.sku.hazmat_class is not None
                and position.sku.sku_id != movement.sku_id
            ):
                existing_hazmat_classes.append(position.sku.hazmat_class)

        for existing_class in existing_hazmat_classes:
            if _are_incompatible(moving_hazmat, existing_class):
                return FeasibilityResult(
                    feasible=False,
                    violations=[
                        ConstraintViolation(
                            constraint_type="hazmat",
                            description=(
                                f"SKU {movement.sku_id} (Class {moving_hazmat.value}) "
                                f"is incompatible with Class {existing_class.value} "
                                f"already in bay {target_aisle}/{target_bay}."
                            ),
                            severity=ConstraintSeverity.HARD,
                        )
                    ],
                )

        return FeasibilityResult(feasible=True, violations=[])
