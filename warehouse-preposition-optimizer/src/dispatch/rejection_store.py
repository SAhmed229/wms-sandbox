"""Redis-backed store for rejected movement decisions.

When an operator rejects a candidate movement, the SKU is suppressed from
re-appearing in scheduler output for a configurable TTL (default 1 hour).
This prevents the optimizer from immediately re-proposing the same move.

Redis key schema
----------------
``rejection:{movement_id}``
    JSON-serialized :class:`RejectionRecord`.  Expires after *ttl_seconds*.

``rejection:sku:{sku_id}``
    Sentinel key ``"1"``.  Expires after *ttl_seconds*.  Scheduler checks this
    key to suppress the whole SKU without knowing the movement UUID.

``rejection:history``
    Redis list of the 200 most-recent rejection JSON payloads (no TTL).
    Used by ``GET /movements/rejected``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "rejection:"
_SKU_PREFIX = "rejection:sku:"
_HISTORY_KEY = "rejection:history"
_MAX_HISTORY = 200


@dataclass
class RejectionRecord:
    """A recorded movement rejection.

    Args:
        movement_id: UUID of the rejected candidate movement.
        sku_id: SKU identifier — used to suppress re-scoring of the same SKU.
        reason: Human-readable reason provided by the operator.
        rejected_at: UTC timestamp when the rejection was recorded.
        ttl_seconds: How long this rejection suppresses the SKU.
    """

    movement_id: str
    sku_id: str
    reason: str
    rejected_at: datetime
    ttl_seconds: int


class RejectionStore:
    """Redis-backed store for operator movement rejections.

    Rejections expire automatically via Redis key TTL so the scheduler
    can re-propose the movement after the suppression window elapses.

    Args:
        redis_client: Connected ``redis.asyncio.Redis`` client.
            Pass ``None`` to disable persistence (all checks return False).
        ttl_seconds: Default suppression window in seconds (default 3600 = 1 h).
    """

    def __init__(self, redis_client: Any, ttl_seconds: int = 3600) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds

    async def record(
        self,
        movement_id: str,
        sku_id: str,
        reason: str,
        ttl_seconds: int | None = None,
    ) -> RejectionRecord:
        """Record a rejection for a candidate movement.

        Args:
            movement_id: UUID string of the rejected candidate.
            sku_id: SKU identifier for the movement.
            reason: Human-readable rejection reason.
            ttl_seconds: Override the default suppression window.

        Returns:
            The created :class:`RejectionRecord`.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl
        now = datetime.now(UTC)
        record = RejectionRecord(
            movement_id=movement_id,
            sku_id=sku_id,
            reason=reason,
            rejected_at=now,
            ttl_seconds=ttl,
        )

        if self._redis is None:
            logger.warning("rejection_store.no_redis", movement_id=movement_id)
            return record

        payload = json.dumps({
            "movement_id": movement_id,
            "sku_id": sku_id,
            "reason": reason,
            "rejected_at": now.isoformat(),
            "ttl_seconds": ttl,
        })

        pipe = self._redis.pipeline()
        pipe.set(f"{_KEY_PREFIX}{movement_id}", payload, ex=ttl)
        pipe.set(f"{_SKU_PREFIX}{sku_id}", "1", ex=ttl)
        pipe.lpush(_HISTORY_KEY, payload)
        pipe.ltrim(_HISTORY_KEY, 0, _MAX_HISTORY - 1)
        await pipe.execute()

        logger.info(
            "rejection_store.recorded",
            movement_id=movement_id,
            sku_id=sku_id,
            ttl_seconds=ttl,
        )
        return record

    async def is_rejected(self, movement_id: str) -> bool:
        """Check if a specific movement is currently rejected.

        Args:
            movement_id: UUID string to check.

        Returns:
            True if the movement is in the store and not expired.
        """
        if self._redis is None:
            return False
        return bool(await self._redis.exists(f"{_KEY_PREFIX}{movement_id}"))

    async def is_sku_suppressed(self, sku_id: str) -> bool:
        """Check if a SKU is suppressed due to a recent rejection.

        Args:
            sku_id: SKU identifier to check.

        Returns:
            True if the SKU is currently suppressed.
        """
        if self._redis is None:
            return False
        return bool(await self._redis.exists(f"{_SKU_PREFIX}{sku_id}"))

    async def get_history(self, limit: int = 50) -> list[RejectionRecord]:
        """Return recent rejection history, newest first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of :class:`RejectionRecord` instances.
        """
        if self._redis is None:
            return []

        capped = min(limit, _MAX_HISTORY)
        raw_items: list[str] = await self._redis.lrange(_HISTORY_KEY, 0, capped - 1)
        records: list[RejectionRecord] = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
                records.append(RejectionRecord(
                    movement_id=data["movement_id"],
                    sku_id=data["sku_id"],
                    reason=data["reason"],
                    rejected_at=datetime.fromisoformat(data["rejected_at"]),
                    ttl_seconds=data["ttl_seconds"],
                ))
            except (KeyError, ValueError):
                pass
        return records

    async def clear(self, movement_id: str) -> bool:
        """Lift a rejection (e.g. after an operator override).

        Args:
            movement_id: UUID string of the movement to un-reject.

        Returns:
            True if the rejection existed and was removed, False if not found.
        """
        if self._redis is None:
            return False

        raw = await self._redis.get(f"{_KEY_PREFIX}{movement_id}")
        if not raw:
            return False

        try:
            data = json.loads(raw)
            sku_id: str = data.get("sku_id", "")
        except (ValueError, KeyError):
            sku_id = ""

        pipe = self._redis.pipeline()
        pipe.delete(f"{_KEY_PREFIX}{movement_id}")
        if sku_id:
            pipe.delete(f"{_SKU_PREFIX}{sku_id}")
        await pipe.execute()

        logger.info("rejection_store.cleared", movement_id=movement_id)
        return True
