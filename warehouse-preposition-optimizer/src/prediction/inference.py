"""Real-time prediction serving with circuit breaker and TTL cache (Phase 2)."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import structlog

from src.models.inventory import InventoryPosition
from src.models.orders import CarrierAppointment, OutboundOrder
from src.prediction.features import FeatureBuilder, HistoricalData
from src.prediction.trainer import MLDemandPredictor

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ──────────────────────────────────────────────────────────────────────────────

class _CircuitState(str, Enum):
    CLOSED = "CLOSED"       # Normal: requests flow through.
    OPEN = "OPEN"           # Failing: requests blocked, fallback used.
    HALF_OPEN = "HALF_OPEN" # Recovery probe: one request allowed through.


@dataclass
class _CircuitBreaker:
    """Simple failure-counting circuit breaker.

    Args:
        failure_threshold: Consecutive failures before opening.
        recovery_timeout_seconds: Seconds before attempting recovery (HALF_OPEN).
    """

    failure_threshold: int = 3
    recovery_timeout_seconds: float = 60.0
    _state: _CircuitState = field(default=_CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)

    @property
    def state(self) -> _CircuitState:
        """Current circuit state, accounting for recovery timeout."""
        if self._state == _CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout_seconds:
                self._state = _CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        """Reset after a successful call."""
        self._failure_count = 0
        self._state = _CircuitState.CLOSED

    def record_failure(self) -> None:
        """Increment failure count; open circuit if threshold reached."""
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._state = _CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "ml_circuit_opened",
                failures=self._failure_count,
                threshold=self.failure_threshold,
            )

    @property
    def is_open(self) -> bool:
        """True when the circuit is OPEN (requests should be blocked)."""
        return self.state == _CircuitState.OPEN


# ──────────────────────────────────────────────────────────────────────────────
# Prediction cache
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _PredictionCache:
    """In-memory TTL cache for prediction results.

    Args:
        ttl_seconds: Time-to-live for cached predictions.
    """

    ttl_seconds: float = 300.0
    _store: dict[str, tuple[float, float]] = field(default_factory=dict, init=False)

    def get(self, key: str) -> float | None:
        """Return cached prediction or None if missing/expired.

        Args:
            key: Cache key derived from feature dict hash.

        Returns:
            Cached probability or None.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self.ttl_seconds:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: float) -> None:
        """Store prediction with current timestamp.

        Args:
            key: Cache key.
            value: Probability to cache.
        """
        self._store[key] = (value, time.monotonic())

    def invalidate(self) -> None:
        """Clear all cached entries."""
        self._store.clear()


# ──────────────────────────────────────────────────────────────────────────────
# Fallback protocol
# ──────────────────────────────────────────────────────────────────────────────

class _FallbackPredictor(Protocol):
    """Protocol for the Phase 1 binary fallback predictor."""

    def predict(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
    ) -> float: ...


# ──────────────────────────────────────────────────────────────────────────────
# Inference engine
# ──────────────────────────────────────────────────────────────────────────────

class InferenceEngine:
    """Wraps MLDemandPredictor with circuit breaker, caching, and Phase 1 fallback.

    When the ML model is unavailable or the circuit breaker trips, prediction
    transparently falls back to the Phase 1 binary lookup.

    Public interface:
        predict(sku_id, appointment, orders, inventory_position, historical_data) -> float
        explain(sku_id, appointment, orders, inventory_position, historical_data) -> dict
    """

    def __init__(
        self,
        ml_predictor: MLDemandPredictor,
        fallback: _FallbackPredictor,
        feature_builder: FeatureBuilder | None = None,
        cache_ttl_seconds: float = 300.0,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
    ) -> None:
        self._ml = ml_predictor
        self._fallback = fallback
        self._features = feature_builder or FeatureBuilder()
        self._cache = _PredictionCache(ttl_seconds=cache_ttl_seconds)
        self._circuit = _CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
        )

    def predict(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
        inventory_position: InventoryPosition | None = None,
        historical_data: HistoricalData | None = None,
    ) -> float:
        """Return load probability, using ML when available or fallback otherwise.

        Args:
            sku_id: SKU identifier.
            appointment: Carrier appointment being evaluated.
            orders: All outbound orders in the horizon.
            inventory_position: Current inventory position (improves feature quality).
            historical_data: Historical demand statistics (improves feature quality).

        Returns:
            Probability in [0.0, 1.0].
        """
        if self._circuit.is_open or not self._ml.is_trained:
            return self._fallback.predict(sku_id, appointment, orders)

        features = self._features.build_features(
            sku_id=sku_id,
            appointment=appointment,
            orders=orders,
            inventory_position=inventory_position,
            historical_data=historical_data,
        )
        cache_key = _hash_features(features)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            prob = self._ml.predict(features)
            self._circuit.record_success()
            self._cache.set(cache_key, prob)
            return prob
        except Exception as exc:
            self._circuit.record_failure()
            logger.warning("ml_prediction_failed", error=str(exc), sku_id=sku_id)
            return self._fallback.predict(sku_id, appointment, orders)

    def explain(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
        inventory_position: InventoryPosition | None = None,
        historical_data: HistoricalData | None = None,
    ) -> dict[str, float]:
        """Return SHAP feature contributions for a prediction.

        Falls back to empty dict if ML is unavailable.

        Args:
            sku_id: SKU identifier.
            appointment: Carrier appointment.
            orders: All outbound orders.
            inventory_position: Current inventory position (optional).
            historical_data: Historical demand statistics (optional).

        Returns:
            Dict of feature_name → SHAP value, or {} if ML unavailable.
        """
        if self._circuit.is_open or not self._ml.is_trained:
            return {}

        features = self._features.build_features(
            sku_id=sku_id,
            appointment=appointment,
            orders=orders,
            inventory_position=inventory_position,
            historical_data=historical_data,
        )
        try:
            return self._ml.explain(features)
        except Exception as exc:
            logger.warning("ml_explain_failed", error=str(exc), sku_id=sku_id)
            return {}

    @property
    def circuit_state(self) -> str:
        """Current circuit breaker state string."""
        return self._circuit.state.value

    def invalidate_cache(self) -> None:
        """Clear prediction cache (e.g. after weight update)."""
        self._cache.invalidate()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _hash_features(features: dict[str, float]) -> str:
    """Produce a stable cache key from a feature dict.

    Args:
        features: Feature dict.

    Returns:
        MD5 hex digest of the sorted, JSON-serialised features.
    """
    serialized = json.dumps(features, sort_keys=True)
    return hashlib.md5(serialized.encode()).hexdigest()  # noqa: S324
