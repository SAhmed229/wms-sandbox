"""AGV fleet interface stub: real AGV API integration goes here."""

from __future__ import annotations

import structlog

from src.models.movements import MovementTask

logger = structlog.get_logger(__name__)

_STUB_RESOURCES = ["AGV-1", "AGV-2", "FORKLIFT-1"]


class AGVInterface:
    """Interface to the AGV fleet manager for task dispatch.

    This is a stub implementation that logs dispatch calls and returns
    placeholder values. Replace with real AGV API calls in production.
    """

    async def dispatch_task(self, task: MovementTask) -> bool:
        """Dispatch a movement task to the AGV fleet manager.

        Args:
            task: The movement task to dispatch.

        Returns:
            True if the fleet manager accepted the task.
        """
        logger.info(
            "agv.dispatch_task",
            movement_id=str(task.movement_id),
            sku_id=task.sku_id,
            from_location=task.from_location.location_id,
            to_location=task.to_location.location_id,
            assigned_resource=task.assigned_resource,
            score=round(task.score, 4),
        )
        # Stub: always accepts
        return True

    async def get_available_resources(self) -> list[str]:
        """Return list of available resource IDs from the fleet manager.

        Returns:
            List of resource identifier strings.
        """
        logger.debug("agv.get_available_resources", resources=_STUB_RESOURCES)
        return list(_STUB_RESOURCES)

    async def get_resource_utilization(self) -> float:
        """Return current fleet utilization fraction.

        Returns:
            Utilization between 0.0 (idle) and 1.0 (fully utilized).
        """
        utilization = 0.0
        logger.debug("agv.get_resource_utilization", utilization=utilization)
        return utilization
