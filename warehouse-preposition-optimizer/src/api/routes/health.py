"""Health and metrics endpoints."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(request: Request) -> dict[str, Any]:
    """Return system health status for all components.

    Checks Redis connectivity, WMS adapter status, queue depth,
    scheduler loop status, and ML inference state.

    Returns:
        Dictionary with overall status and per-component details.
    """
    components: dict[str, Any] = {"api": "ok"}
    overall = "ok"

    # Redis
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is not None:
        try:
            await redis_client.ping()
            components["redis"] = "ok"
        except Exception as exc:
            components["redis"] = f"error: {exc}"
            overall = "degraded"
    else:
        components["redis"] = "unavailable"
        overall = "degraded"

    # Queue depth
    task_queue = getattr(request.app.state, "task_queue", None)
    if task_queue is not None and redis_client is not None:
        try:
            depth = await task_queue.get_queue_depth()
            components["queue_depth"] = depth
        except Exception:
            components["queue_depth"] = "unknown"
    else:
        components["queue_depth"] = "unknown"

    # WMS adapter
    wms_adapter = getattr(request.app.state, "wms_adapter", None)
    if wms_adapter is not None:
        components["wms_adapter"] = getattr(wms_adapter, "_connected", "unknown")
    else:
        components["wms_adapter"] = "not_initialised"
        overall = "degraded"

    # ML inference
    ml_inference = getattr(request.app.state, "ml_inference", None)
    if ml_inference is not None:
        components["ml_inference"] = {
            "active": True,
            "circuit_state": ml_inference.circuit_state,
        }
    else:
        components["ml_inference"] = {"active": False, "mode": "phase1_binary"}

    # OR-Tools optimiser
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        components["or_optimisation"] = getattr(settings, "use_or_optimization", False)

    return {"status": overall, "components": components}


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """Return Prometheus metrics in text exposition format.

    Returns:
        Prometheus-formatted metrics as plain text.
    """
    content = generate_latest()
    return PlainTextResponse(content=content, media_type=CONTENT_TYPE_LATEST)
