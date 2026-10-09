"""WebSocket connection manager and real-time movement event broadcaster.

Clients connect to ``/api/v1/ws/movements`` and receive a JSON stream of
warehouse events as the optimizer scores, dispatches, and tracks movements.

Authentication
--------------
Pass the API key as a query parameter::

    ws://host/api/v1/ws/movements?api_key=YOUR_KEY

The WebSocket HTTP upgrade is rejected with code 1008 (policy violation) if
the key is missing or wrong.

Event format
------------
Every message is a JSON object::

    {
        "event": "task_dispatched",
        "timestamp": "2024-01-01T00:00:00+00:00",
        "data": { ... }
    }

Event types
-----------
``cycle_complete``
    Emitted after each scheduler cycle.
    ``data``: ``{candidates_scored, tasks_dispatched, reason}``

``task_dispatched``
    Emitted when a single task is pushed to the queue.
    ``data``: ``{movement_id, sku_id, score, from_location_id, to_location_id}``

``task_status_changed``
    Emitted when a task transitions status (acknowledge, complete, cancel).
    ``data``: ``{movement_id, sku_id, old_status, new_status}``

``movement_rejected``
    Emitted when an operator rejects a candidate.
    ``data``: ``{movement_id, sku_id, reason, ttl_seconds}``

Keep-alive
----------
Send the text ``"ping"`` to receive a ``"pong"`` reply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events to all of them.

    This is an in-process pub/sub; it works without Redis.  For multi-process
    deployments, replace ``broadcast`` with a Redis Pub/Sub fanout.
    """

    def __init__(self) -> None:
        self._active: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection.

        Args:
            websocket: The WebSocket to register.
        """
        await websocket.accept()
        self._active.append(websocket)
        logger.debug("ws.connected", total=len(self._active))

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from the active registry.

        Args:
            websocket: The WebSocket to deregister.
        """
        try:
            self._active.remove(websocket)
        except ValueError:
            pass
        logger.debug("ws.disconnected", total=len(self._active))

    async def broadcast(self, event: str, data: dict[str, Any]) -> None:
        """Push an event to all connected clients.

        Connections that raise an exception during send are silently removed.

        Args:
            event: Event type string (e.g. ``"task_dispatched"``).
            data: Arbitrary JSON-serializable payload dict.
        """
        if not self._active:
            return

        message = {
            "event": event,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": data,
        }
        dead: list[WebSocket] = []
        for ws in list(self._active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def connection_count(self) -> int:
        """Number of currently active connections."""
        return len(self._active)


@router.websocket("/ws/movements")
async def websocket_movements(websocket: WebSocket) -> None:
    """Real-time movement event stream.

    Connect with ``?api_key=YOUR_KEY``.  Receives JSON events pushed by the
    optimizer.  Send ``"ping"`` for a ``"pong"`` keep-alive reply.
    """
    settings = getattr(websocket.app.state, "settings", None)
    if settings is not None:
        expected = settings.api_key
        received = websocket.query_params.get("api_key", "")
        if received != expected:
            await websocket.close(code=1008, reason="Invalid API key")
            return

    manager: ConnectionManager = websocket.app.state.ws_manager
    await manager.connect(websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.warning("ws.error", error=str(exc))
        manager.disconnect(websocket)
