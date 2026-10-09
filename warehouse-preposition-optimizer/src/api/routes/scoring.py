"""Scoring explanation endpoints."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/scoring", tags=["scoring"])


def _get_scheduler(request: Request) -> Any:
    """Extract scheduler from app state.

    Args:
        request: FastAPI request.

    Returns:
        PrePositionScheduler instance.
    """
    return request.app.state.scheduler


@router.get("/explain/{movement_id}")
async def explain_score(
    movement_id: UUID,
    scheduler: Annotated[Any, Depends(_get_scheduler)],
) -> dict[str, Any]:
    """Return detailed score breakdown for a candidate movement.

    Includes all V(m) components and, when Phase 2 ML is active, per-feature
    SHAP values (prefixed with ``shap_``) that explain the P_load estimate.

    Args:
        movement_id: UUID of the candidate to explain.

    Returns:
        Score breakdown dictionary with keys:
        - movement_id, sku_id, score, reason, from_location, to_location
        - score_components: V(m) terms plus optional shap_* contributions
        - ml_active: whether Phase 2 ML prediction was used
        - shap_contributions: dict of feature → SHAP value (empty if Phase 1)
    """
    candidates = await scheduler.generate_candidates()
    candidate = next(
        (c for c in candidates if c.movement_id == movement_id), None
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Candidate {movement_id} not found.")

    # Separate core V(m) terms from SHAP contributions stored in score_components
    core_keys = {"t_saved", "p_load", "w_order", "c_move", "c_opportunity", "numerator", "denominator"}
    core_components = {k: v for k, v in candidate.score_components.items() if k in core_keys}
    shap_contributions = {
        k[len("shap_"):]: v
        for k, v in candidate.score_components.items()
        if k.startswith("shap_")
    }

    return {
        "movement_id": str(movement_id),
        "sku_id": candidate.sku_id,
        "score": candidate.score,
        "score_components": core_components,
        "shap_contributions": shap_contributions,
        "ml_active": bool(shap_contributions),
        "reason": candidate.reason,
        "from_location": candidate.from_location.location_id,
        "to_location": candidate.to_location.location_id,
    }
