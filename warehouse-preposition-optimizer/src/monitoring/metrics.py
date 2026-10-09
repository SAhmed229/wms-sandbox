"""Prometheus metrics definitions for the optimizer."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

MOVEMENTS_SCORED: Counter = Counter(
    "movements_scored_total",
    "Total number of candidate movements scored.",
)

MOVEMENTS_DISPATCHED: Counter = Counter(
    "movements_dispatched_total",
    "Total number of movement tasks dispatched to the queue.",
)

MOVEMENTS_COMPLETED: Counter = Counter(
    "movements_completed_total",
    "Total number of movement tasks completed.",
)

AVG_SCORE: Gauge = Gauge(
    "avg_score",
    "Rolling average score of the most recent scoring cycle.",
)

QUEUE_DEPTH: Gauge = Gauge(
    "queue_depth",
    "Current number of pending movement tasks in the queue.",
)

WMS_POLL_DURATION: Histogram = Histogram(
    "wms_poll_duration_seconds",
    "Duration of WMS data poll operations.",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

CONSTRAINT_VIOLATIONS: Counter = Counter(
    "constraint_violations_total",
    "Total number of constraint violations detected.",
    labelnames=["constraint_type"],
)
