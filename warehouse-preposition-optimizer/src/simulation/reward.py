"""Reward function definitions for the warehouse pre-positioning RL agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RewardWeights:
    """Configurable weights for each reward component.

    Args:
        r1_seconds_saved: Reward per second saved during truck loading (primary signal).
        r2_movement_cost: Penalty per movement executed (seconds of forklift time).
        r3_early_departure: Bonus per second truck departs early.
        r4_late_departure: Penalty per second truck departs late.
        r5_distance_shaping: Small positive per-step reward for reducing average
            distance-to-dock of ordered inventory (exploration shaping).
    """

    r1_seconds_saved: float = 1.0
    r2_movement_cost: float = 0.05
    r3_early_departure: float = 0.5
    r4_late_departure: float = 2.0
    r5_distance_shaping: float = 0.01


@dataclass
class EpisodeMetrics:
    """Metrics accumulated over one episode (one 8-hour shift).

    Args:
        total_seconds_saved: Sum of loading time saved by pre-positioning.
        total_movement_cost_seconds: Total forklift seconds spent on repositioning.
        trucks_served: Number of carrier appointments that loaded at least one pallet.
        early_departure_seconds: Total seconds trucks departed before scheduled_departure.
        late_departure_seconds: Total seconds trucks departed after scheduled_departure.
        movements_executed: Total repositioning movements completed.
        movements_rejected: Movements skipped due to infeasibility during simulation.
    """

    total_seconds_saved: float = 0.0
    total_movement_cost_seconds: float = 0.0
    trucks_served: int = 0
    early_departure_seconds: float = 0.0
    late_departure_seconds: float = 0.0
    movements_executed: int = 0
    movements_rejected: int = 0
    avg_dock_dwell_seconds: float = 0.0
    pre_stage_hit_rate: float = 0.0  # fraction of loads that came from staging


def compute_step_reward(
    seconds_saved: float,
    movement_cost_seconds: float,
    weights: RewardWeights,
) -> float:
    """Compute per-step reward for a single pre-positioning movement.

    Called once per movement dispatch decision.

    Args:
        seconds_saved: Estimated seconds saved for the movement (T_saved from V(m)).
            May be 0 if the move doesn't directly contribute to loading time savings.
        movement_cost_seconds: Actual forklift time consumed by the movement.
        weights: Reward component weights.

    Returns:
        Scalar reward for this step.
    """
    reward = weights.r1_seconds_saved * seconds_saved
    reward -= weights.r2_movement_cost * movement_cost_seconds
    return reward


def compute_truck_departure_reward(
    actual_departure_seconds: float,
    scheduled_departure_seconds: float,
    weights: RewardWeights,
) -> float:
    """Compute reward/penalty at truck departure event.

    Called once per truck departure during simulation.

    Args:
        actual_departure_seconds: Sim time (seconds from epoch) when truck departed.
        scheduled_departure_seconds: Scheduled departure time in sim seconds.
        weights: Reward component weights.

    Returns:
        Positive reward for early departure, negative penalty for late departure.
    """
    delta = scheduled_departure_seconds - actual_departure_seconds
    if delta >= 0:
        return weights.r3_early_departure * delta
    return weights.r4_late_departure * delta  # negative delta → negative reward


def compute_shaping_reward(
    avg_distance_before: float,
    avg_distance_after: float,
    weights: RewardWeights,
) -> float:
    """Compute potential-based shaping reward for reducing average distance-to-dock.

    Implements Ng et al. potential-based shaping: Φ(s') − Φ(s) where
    Φ(s) = −avg_distance_to_dock (higher potential = closer to dock).

    Args:
        avg_distance_before: Mean Manhattan distance of ordered SKUs to dock before step.
        avg_distance_after: Mean Manhattan distance after step.
        weights: Reward component weights.

    Returns:
        Shaping reward (positive if distance decreased).
    """
    improvement = avg_distance_before - avg_distance_after
    return weights.r5_distance_shaping * improvement


def compute_episode_return(metrics: EpisodeMetrics, weights: RewardWeights) -> float:
    """Compute total undiscounted return for a completed episode.

    Useful for evaluation and logging.

    Args:
        metrics: Accumulated episode metrics from the digital twin.
        weights: Reward component weights.

    Returns:
        Total episode return.
    """
    ret = weights.r1_seconds_saved * metrics.total_seconds_saved
    ret -= weights.r2_movement_cost * metrics.total_movement_cost_seconds
    ret += weights.r3_early_departure * metrics.early_departure_seconds
    ret -= weights.r4_late_departure * metrics.late_departure_seconds
    return ret
