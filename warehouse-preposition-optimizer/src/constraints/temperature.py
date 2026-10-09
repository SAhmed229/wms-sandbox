"""Temperature zone constraint: SKU must be stored in a compatible zone."""

from __future__ import annotations

from src.constraints.feasibility import ConstraintFilter
from src.ingestion.wms_adapter import WarehouseState
from src.models.constraints import ConstraintSeverity, ConstraintViolation, FeasibilityResult
from src.models.inventory import TemperatureZone
from src.models.movements import CandidateMovement

# Mapping of SKU required zone to the set of acceptable target zones.
# CHILLED SKU may go to CHILLED or FROZEN (colder is ok for refrigerated product).
# AMBIENT must stay AMBIENT, FROZEN must stay FROZEN.
_COMPATIBLE_TARGET_ZONES: dict[TemperatureZone, frozenset[TemperatureZone]] = {
    TemperatureZone.AMBIENT: frozenset({TemperatureZone.AMBIENT}),
    TemperatureZone.CHILLED: frozenset({TemperatureZone.CHILLED, TemperatureZone.FROZEN}),
    TemperatureZone.FROZEN: frozenset({TemperatureZone.FROZEN}),
}


class TemperatureConstraint(ConstraintFilter):
    """HARD constraint: SKU temperature zone must be compatible with target location.

    Rules:
    - AMBIENT SKU must go to AMBIENT zone only.
    - CHILLED SKU may go to CHILLED or FROZEN (colder storage is acceptable).
    - FROZEN SKU must go to FROZEN zone only.
    """

    def check(
        self, movement: CandidateMovement, state: WarehouseState
    ) -> FeasibilityResult:
        """Check temperature zone compatibility for the movement.

        Args:
            movement: The candidate movement to evaluate.
            state: Current warehouse state (used to resolve SKU zone).

        Returns:
            FeasibilityResult indicating compatibility.
        """
        sku_zone: TemperatureZone | None = None
        for position in state.inventory_positions:
            if position.sku.sku_id == movement.sku_id:
                sku_zone = position.sku.requires_temperature_zone
                break

        if sku_zone is None:
            return FeasibilityResult(
                feasible=True,
                violations=[
                    ConstraintViolation(
                        constraint_type="temperature",
                        description=(
                            f"SKU {movement.sku_id} not found in state; "
                            "temperature zone unknown."
                        ),
                        severity=ConstraintSeverity.SOFT,
                    )
                ],
            )

        target_zone = movement.to_location.temperature_zone
        allowed = _COMPATIBLE_TARGET_ZONES.get(sku_zone, frozenset({sku_zone}))

        if target_zone in allowed:
            return FeasibilityResult(feasible=True, violations=[])

        return FeasibilityResult(
            feasible=False,
            violations=[
                ConstraintViolation(
                    constraint_type="temperature",
                    description=(
                        f"SKU {movement.sku_id} requires {sku_zone.value} zone "
                        f"but target location {movement.to_location.location_id} "
                        f"is {target_zone.value} (incompatible)."
                    ),
                    severity=ConstraintSeverity.HARD,
                )
            ],
        )
