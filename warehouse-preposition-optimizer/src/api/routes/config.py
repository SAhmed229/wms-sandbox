"""Scoring weight configuration endpoints."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request

from src.scoring.weights import ScoringWeights

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/config", tags=["config"])

# In-memory weights store; persists for process lifetime.
# For multi-instance deployments, back this with Redis.
_current_weights = ScoringWeights()


@router.get("/weights", response_model=ScoringWeights)
async def get_weights() -> ScoringWeights:
    """Return current scoring weights configuration.

    Returns:
        Current ScoringWeights instance.
    """
    return _current_weights


@router.put("/weights", response_model=ScoringWeights)
async def update_weights(weights: ScoringWeights, request: Request) -> ScoringWeights:
    """Update scoring weights at runtime without restart.

    Also invalidates the ML inference cache so the next cycle uses the new
    weights for opportunity-cost calculations immediately.

    Args:
        weights: New ScoringWeights to apply.

    Returns:
        Updated ScoringWeights instance.
    """
    global _current_weights
    _current_weights = weights

    # Push new weights into the live scorer instance.
    if hasattr(request.app.state, "scorer"):
        request.app.state.scorer._weights = weights

    # Invalidate ML prediction cache — cached probabilities were built with
    # the old context and may rank differently under new weights.
    ml_inference = getattr(request.app.state, "ml_inference", None)
    if ml_inference is not None:
        ml_inference.invalidate_cache()
        logger.info("config.ml_cache_invalidated")

    logger.info("config.weights_updated", weights=weights.model_dump())
    return _current_weights
