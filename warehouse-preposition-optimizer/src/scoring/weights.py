"""Scoring weight configuration model."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScoringWeights(BaseModel):
    """Weights and parameters for the movement value function V(m).

    All weight fields must be non-negative. The decay_constant_seconds
    controls how quickly order urgency grows as the cutoff approaches.

    Args:
        time_saved: Weight applied to the T_saved term.
        load_probability: Weight applied to the P_load term.
        order_priority: Weight applied to the W_order term.
        movement_cost: Weight applied to the C_move denominator term.
        opportunity_cost: Weight applied to the C_opportunity denominator term.
        decay_constant_seconds: Exponential decay constant for urgency (seconds).
    """

    time_saved: float = Field(default=1.0, ge=0)
    load_probability: float = Field(default=1.0, ge=0)
    order_priority: float = Field(default=1.0, ge=0)
    movement_cost: float = Field(default=1.0, ge=0)
    opportunity_cost: float = Field(default=1.0, ge=0)
    decay_constant_seconds: float = Field(default=3600.0, gt=0)
