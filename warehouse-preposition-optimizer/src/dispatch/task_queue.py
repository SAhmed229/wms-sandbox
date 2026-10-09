"""Redis-backed task queue for movement dispatch."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.models.movements import MovementStatus, MovementTask
from src.monitoring.metrics import MOVEMENTS_COMPLETED, QUEUE_DEPTH

logger = structlog.get_logger(__name__)

_PENDING_SET_KEY = "movements:pending"
_DATA_KEY_PREFIX = "movements:data:"
_EXPIRY_KEY_PREFIX = "movements:expiry:"


class TaskQueue:
    """Redis-backed queue for movement tasks using sorted sets.

    Tasks are stored in a sorted set keyed by score (descending).
    Task data is stored as JSON hashes. Expiry is tracked via TTL keys.

    Args:
        redis_client: Connected redis.asyncio.Redis client.
        task_expiry_minutes: Default expiry time for pending tasks.
    """

    def __init__(self, redis_client: Any, task_expiry_minutes: int = 15) -> None:
        self._redis = redis_client
        self._expiry_minutes = task_expiry_minutes

    async def push(self, task: MovementTask) -> None:
        """Add a task to the pending queue.

        Stores task data as JSON and adds to sorted set by score (higher = better).

        Args:
            task: The movement task to enqueue.

        Returns:
            None
        """
        task_id = str(task.movement_id)
        data_key = f"{_DATA_KEY_PREFIX}{task_id}"
        expiry_key = f"{_EXPIRY_KEY_PREFIX}{task_id}"
        expiry_seconds = self._expiry_minutes * 60

        task_json = task.model_dump_json()

        pipe = self._redis.pipeline()
        # Store task data
        pipe.set(data_key, task_json, ex=expiry_seconds + 60)
        # Add to sorted set with score as the sort key
        pipe.zadd(_PENDING_SET_KEY, {task_id: task.score})
        # Store expiry marker
        pipe.set(expiry_key, "1", ex=expiry_seconds)
        await pipe.execute()

        logger.debug("task_queue.push", task_id=task_id, score=task.score)

    async def pop(self, n: int = 1) -> list[MovementTask]:
        """Get the top N tasks by score from the pending queue.

        Args:
            n: Number of tasks to retrieve.

        Returns:
            List of MovementTask instances, highest score first.
        """
        # Get top N task IDs by score (descending)
        task_ids: list[str] = await self._redis.zrevrange(_PENDING_SET_KEY, 0, n - 1)
        tasks: list[MovementTask] = []

        for task_id in task_ids:
            task_id_str = task_id if isinstance(task_id, str) else task_id.decode()
            data_key = f"{_DATA_KEY_PREFIX}{task_id_str}"
            raw = await self._redis.get(data_key)
            if raw:
                task_data = json.loads(raw)
                tasks.append(MovementTask.model_validate(task_data))

        return tasks

    async def update_status(self, movement_id: str, status: MovementStatus) -> None:
        """Update the status of a task in the queue.

        Args:
            movement_id: The UUID string of the movement to update.
            status: The new MovementStatus.

        Returns:
            None
        """
        data_key = f"{_DATA_KEY_PREFIX}{movement_id}"
        raw = await self._redis.get(data_key)
        if not raw:
            logger.warning("task_queue.update_not_found", movement_id=movement_id)
            return

        task_data: dict[str, Any] = json.loads(raw)
        task_data["status"] = status.value
        if status == MovementStatus.COMPLETED:
            task_data["completed_at"] = datetime.now(UTC).isoformat()

        await self._redis.set(data_key, json.dumps(task_data))

        # Remove from pending set if terminal status
        if status in (MovementStatus.COMPLETED, MovementStatus.CANCELLED):
            await self._redis.zrem(_PENDING_SET_KEY, movement_id)
            if status == MovementStatus.COMPLETED:
                MOVEMENTS_COMPLETED.inc()

        # Update queue depth gauge
        depth = await self._redis.zcard(_PENDING_SET_KEY)
        QUEUE_DEPTH.set(depth)

        logger.debug("task_queue.status_updated", movement_id=movement_id, status=status.value)

    async def get_active_tasks(self) -> list[MovementTask]:
        """Return all tasks currently in the pending sorted set.

        Returns:
            List of MovementTask instances with PENDING or IN_PROGRESS status.
        """
        task_ids: list[str] = await self._redis.zrevrange(_PENDING_SET_KEY, 0, -1)
        tasks: list[MovementTask] = []

        for task_id in task_ids:
            task_id_str = task_id if isinstance(task_id, str) else task_id.decode()
            data_key = f"{_DATA_KEY_PREFIX}{task_id_str}"
            raw = await self._redis.get(data_key)
            if raw:
                tasks.append(MovementTask.model_validate(json.loads(raw)))

        return tasks

    async def expire_old_tasks(self, expiry_minutes: int | None = None) -> int:
        """Mark PENDING tasks whose expiry key has elapsed as CANCELLED.

        Args:
            expiry_minutes: Override the default expiry window.

        Returns:
            Number of tasks cancelled.
        """
        timeout = expiry_minutes or self._expiry_minutes
        task_ids: list[str] = await self._redis.zrevrange(_PENDING_SET_KEY, 0, -1)
        cancelled_count = 0

        for task_id in task_ids:
            task_id_str = task_id if isinstance(task_id, str) else task_id.decode()
            expiry_key = f"{_EXPIRY_KEY_PREFIX}{task_id_str}"
            expiry_exists = await self._redis.exists(expiry_key)

            if not expiry_exists:
                # Expiry key has elapsed — cancel the task
                await self.update_status(task_id_str, MovementStatus.CANCELLED)
                cancelled_count += 1
                logger.info("task_queue.task_expired", task_id=task_id_str)

        return cancelled_count

    async def get_task(self, movement_id: str) -> MovementTask | None:
        """Retrieve a single task by movement ID.

        Args:
            movement_id: UUID string of the movement to look up.

        Returns:
            :class:`MovementTask` if found, ``None`` otherwise.
        """
        data_key = f"{_DATA_KEY_PREFIX}{movement_id}"
        raw = await self._redis.get(data_key)
        if not raw:
            return None
        return MovementTask.model_validate(json.loads(raw))

    async def get_queue_depth(self) -> int:
        """Return the number of tasks currently in the pending queue.

        Returns:
            Count of tasks in the sorted set.
        """
        return await self._redis.zcard(_PENDING_SET_KEY)
