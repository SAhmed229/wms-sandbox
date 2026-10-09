"""Tests for Phase 2 ML demand prediction components."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.models.inventory import ABCClass, InventoryPosition, Location, SKU, TemperatureZone
from src.models.orders import AppointmentStatus, CarrierAppointment, OrderLine, OutboundOrder
from src.prediction.features import FEATURE_NAMES, FeatureBuilder, HistoricalData
from src.prediction.inference import InferenceEngine, _CircuitBreaker, _CircuitState
from src.prediction.trainer import MLDemandPredictor


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_appointment(hours: float = 2.0, dock_door: int = 1) -> CarrierAppointment:
    now = datetime.now(UTC)
    return CarrierAppointment(
        appointment_id="APPT-TEST",
        carrier="FedEx",
        dock_door=dock_door,
        scheduled_arrival=now + timedelta(hours=hours),
        scheduled_departure=now + timedelta(hours=hours + 1),
        status=AppointmentStatus.SCHEDULED,
    )


def _make_order(
    appointment: CarrierAppointment,
    sku_id: str,
    priority: int = 5,
    cutoff_hours: float = 2.5,
    picked: bool = False,
) -> OutboundOrder:
    now = datetime.now(UTC)
    return OutboundOrder(
        order_id="ORD-TEST",
        appointment=appointment,
        lines=[OrderLine(line_id="L1", sku_id=sku_id, quantity=10, picked=picked)],
        priority=priority,
        cutoff_time=now + timedelta(hours=cutoff_hours),
    )


def _make_inventory_position(
    sku_id: str,
    abc_class: ABCClass = ABCClass.A,
    quantity: int = 50,
    dock_door: int | None = None,
) -> InventoryPosition:
    loc = Location(
        location_id=f"LOC-{sku_id}",
        zone="A",
        aisle=1,
        bay=1,
        level=0,
        x=10.0,
        y=5.0,
        nearest_dock_door=dock_door,
    )
    sku = SKU(
        sku_id=sku_id,
        description="Test",
        weight_kg=50.0,
        volume_m3=0.3,
        abc_class=abc_class,
    )
    return InventoryPosition(position_id=f"POS-{sku_id}", sku=sku, location=loc, quantity=quantity)


def _make_synthetic_df(n_rows: int = 200, seed: int = 0) -> "pd.DataFrame":
    """Generate a small synthetic training DataFrame."""
    import pandas as pd

    rng = np.random.default_rng(seed)
    data = {name: rng.uniform(0, 1, n_rows).tolist() for name in FEATURE_NAMES}
    # Make it slightly separable so AUC > 0.5
    data["order_exists_for_sku"] = rng.choice([0.0, 1.0], size=n_rows).tolist()
    was_loaded = (
        np.array(data["order_exists_for_sku"]) * 0.7
        + rng.uniform(0, 0.3, n_rows)
        > 0.5
    ).astype(int).tolist()
    data["was_loaded"] = was_loaded
    return pd.DataFrame(data)


# ──────────────────────────────────────────────────────────────────────────────
# FeatureBuilder tests
# ──────────────────────────────────────────────────────────────────────────────

def test_feature_builder_produces_correct_keys() -> None:
    """build_features() must return exactly the FEATURE_NAMES keys."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    features = fb.build_features(sku_id="SKU-1", appointment=appt, orders=[])

    assert set(features.keys()) == set(FEATURE_NAMES)
    assert len(features) == len(FEATURE_NAMES)


def test_feature_builder_no_nulls() -> None:
    """All feature values must be floats — no None, NaN, or inf."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    features = fb.build_features(
        sku_id="SKU-1",
        appointment=appt,
        orders=[],
        historical_data=None,
        inventory_position=None,
    )

    for name, val in features.items():
        assert isinstance(val, float), f"{name} is not float"
        assert math.isfinite(val), f"{name} is not finite: {val}"


def test_feature_builder_no_historical_data_uses_defaults() -> None:
    """Missing historical data should not raise; defaults should be 0 or 30."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    features = fb.build_features(sku_id="SKU-UNKNOWN", appointment=appt, orders=[])

    assert features["avg_daily_demand_30d"] == 0.0
    assert features["demand_cv_30d"] == 0.0
    assert features["days_since_last_shipment"] == 30.0


def test_feature_builder_order_exists_flag_set_when_order_present() -> None:
    """order_exists_for_sku must be 1.0 when an order for the SKU exists."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    order = _make_order(appt, sku_id="SKU-A")

    features = fb.build_features(sku_id="SKU-A", appointment=appt, orders=[order])

    assert features["order_exists_for_sku"] == 1.0
    assert features["order_priority"] == float(order.priority)


def test_feature_builder_order_exists_false_for_different_sku() -> None:
    """order_exists_for_sku must be 0.0 when SKU not on any order."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    order = _make_order(appt, sku_id="SKU-B")  # different SKU

    features = fb.build_features(sku_id="SKU-A", appointment=appt, orders=[order])

    assert features["order_exists_for_sku"] == 0.0


def test_feature_builder_cyclical_encoding_in_range() -> None:
    """Sin/cos temporal features must be in [-1, 1]."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    now = datetime(2024, 6, 15, 14, 30, tzinfo=UTC)
    features = fb.build_features(sku_id="SKU-1", appointment=appt, orders=[], now=now)

    for key in ("hour_of_day_sin", "hour_of_day_cos", "day_of_week_sin", "day_of_week_cos"):
        assert -1.0 <= features[key] <= 1.0, f"{key} = {features[key]} out of range"


def test_feature_builder_abc_class_ordinal() -> None:
    """ABC class A → 3, B → 2, C → 1."""
    fb = FeatureBuilder()
    appt = _make_appointment()

    pos_a = _make_inventory_position("SKU-A", abc_class=ABCClass.A)
    pos_b = _make_inventory_position("SKU-B", abc_class=ABCClass.B)
    pos_c = _make_inventory_position("SKU-C", abc_class=ABCClass.C)

    f_a = fb.build_features("SKU-A", appt, [], inventory_position=pos_a)
    f_b = fb.build_features("SKU-B", appt, [], inventory_position=pos_b)
    f_c = fb.build_features("SKU-C", appt, [], inventory_position=pos_c)

    assert f_a["abc_class_ordinal"] == 3.0
    assert f_b["abc_class_ordinal"] == 2.0
    assert f_c["abc_class_ordinal"] == 1.0


def test_feature_builder_dock_zone_match_when_same_door() -> None:
    """dock_zone_match should be 1.0 when inventory is near the appointment's dock door."""
    fb = FeatureBuilder()
    appt = _make_appointment(dock_door=3)
    pos = _make_inventory_position("SKU-1", dock_door=3)  # same door

    features = fb.build_features("SKU-1", appt, [], inventory_position=pos)

    assert features["dock_zone_match"] == 1.0


def test_feature_builder_minutes_until_cutoff_computed() -> None:
    """minutes_until_cutoff should reflect order cutoff time."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    now = datetime.now(UTC)
    order = _make_order(appt, "SKU-1", cutoff_hours=3.0)  # 180 minutes out

    features = fb.build_features("SKU-1", appt, [order], now=now)

    # Should be approximately 180 (within a few seconds of test execution)
    assert 170.0 < features["minutes_until_cutoff"] < 190.0


def test_feature_builder_historical_data_populated() -> None:
    """Historical demand fields should use HistoricalData values when provided."""
    fb = FeatureBuilder()
    appt = _make_appointment()
    hist = HistoricalData(
        avg_daily_demand={"SKU-X": 42.0},
        demand_cv={"SKU-X": 0.75},
        days_since_last_shipment={"SKU-X": 7.0},
        carrier_sku_frequency={("FedEx", "SKU-X"): 0.6},
        carrier_id_encoding={"FedEx": 5},
    )

    features = fb.build_features("SKU-X", appt, [], historical_data=hist)

    assert features["avg_daily_demand_30d"] == 42.0
    assert features["demand_cv_30d"] == 0.75
    assert features["days_since_last_shipment"] == 7.0
    assert features["carrier_sku_frequency"] == 0.6
    assert features["carrier_id_encoded"] == 5.0


# ──────────────────────────────────────────────────────────────────────────────
# MLDemandPredictor tests
# ──────────────────────────────────────────────────────────────────────────────

def test_ml_predictor_raises_before_training() -> None:
    """predict() must raise RuntimeError before train() is called."""
    predictor = MLDemandPredictor()
    features = {name: 0.0 for name in FEATURE_NAMES}

    with pytest.raises(RuntimeError, match="not trained"):
        predictor.predict(features)


def test_ml_predictor_explain_raises_before_training() -> None:
    """explain() must raise RuntimeError before train() is called."""
    predictor = MLDemandPredictor()
    features = {name: 0.0 for name in FEATURE_NAMES}

    with pytest.raises(RuntimeError, match="not trained"):
        predictor.explain(features)


def test_ml_predictor_train_and_predict(tmp_path: "Path") -> None:
    """Train on synthetic data and verify predict() returns a valid probability."""
    df = _make_synthetic_df(n_rows=300)
    predictor = MLDemandPredictor()
    metrics = predictor.train(df, n_trials=3, cv_folds=2)  # fast run for tests

    assert predictor.is_trained
    assert "cv_auc_mean" in metrics
    assert 0.0 <= metrics["cv_auc_mean"] <= 1.0

    features = {name: 0.5 for name in FEATURE_NAMES}
    prob = predictor.predict(features)

    assert 0.0 <= prob <= 1.0


def test_ml_predictor_explain_returns_all_features(tmp_path: "Path") -> None:
    """explain() must return a SHAP value for every FEATURE_NAMES entry."""
    df = _make_synthetic_df(n_rows=300)
    predictor = MLDemandPredictor()
    predictor.train(df, n_trials=3, cv_folds=2)

    features = {name: float(i) * 0.1 for i, name in enumerate(FEATURE_NAMES)}
    shap_vals = predictor.explain(features)

    assert set(shap_vals.keys()) == set(FEATURE_NAMES)
    for val in shap_vals.values():
        assert isinstance(val, float)


def test_ml_predictor_save_and_load(tmp_path: "Path") -> None:
    """Model persisted with save() must produce same predictions after load()."""
    df = _make_synthetic_df(n_rows=200)
    predictor = MLDemandPredictor()
    predictor.train(df, n_trials=2, cv_folds=2)

    model_path = tmp_path / "model.pkl"
    predictor.save(model_path)

    loaded = MLDemandPredictor()
    loaded.load(model_path)

    features = {name: 0.3 for name in FEATURE_NAMES}
    assert abs(predictor.predict(features) - loaded.predict(features)) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# Circuit breaker tests
# ──────────────────────────────────────────────────────────────────────────────

def test_circuit_breaker_opens_after_threshold_failures() -> None:
    """Circuit should open after failure_threshold consecutive failures."""
    cb = _CircuitBreaker(failure_threshold=3)

    assert cb.state == _CircuitState.CLOSED
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open  # Not yet open after 2 failures
    cb.record_failure()
    assert cb.is_open  # Open after 3 failures


def test_circuit_breaker_resets_on_success() -> None:
    """Circuit should return to CLOSED after a successful call."""
    cb = _CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open

    cb.record_success()
    assert not cb.is_open
    assert cb.state == _CircuitState.CLOSED


# ──────────────────────────────────────────────────────────────────────────────
# InferenceEngine tests
# ──────────────────────────────────────────────────────────────────────────────

def _make_trained_engine(n_rows: int = 300) -> InferenceEngine:
    """Build an InferenceEngine with a trained MLDemandPredictor."""
    from src.scoring.demand_predictor import DemandPredictor

    df = _make_synthetic_df(n_rows=n_rows)
    ml = MLDemandPredictor()
    ml.train(df, n_trials=3, cv_folds=2)
    return InferenceEngine(
        ml_predictor=ml,
        fallback=DemandPredictor(),
        cache_ttl_seconds=300.0,
    )


def test_inference_engine_returns_float_in_range() -> None:
    """predict() must return a value in [0.0, 1.0]."""
    engine = _make_trained_engine()
    appt = _make_appointment()
    order = _make_order(appt, "SKU-1")

    prob = engine.predict("SKU-1", appt, [order])

    assert 0.0 <= prob <= 1.0


def test_inference_engine_caches_predictions() -> None:
    """Two identical calls must return the same value (cache hit)."""
    engine = _make_trained_engine()
    appt = _make_appointment()
    order = _make_order(appt, "SKU-CACHE")

    p1 = engine.predict("SKU-CACHE", appt, [order])
    p2 = engine.predict("SKU-CACHE", appt, [order])

    assert p1 == p2


def test_inference_engine_falls_back_when_circuit_open() -> None:
    """When circuit is open, engine must fall back to Phase 1 binary lookup."""
    from src.scoring.demand_predictor import DemandPredictor

    # Use a mock ML predictor that is "trained" but always raises on predict
    mock_ml = MagicMock(spec=MLDemandPredictor)
    mock_ml.is_trained = True
    mock_ml.predict.side_effect = RuntimeError("model broken")

    engine = InferenceEngine(
        ml_predictor=mock_ml,
        fallback=DemandPredictor(),
        failure_threshold=1,
    )
    appt = _make_appointment()
    order = _make_order(appt, "SKU-FALLBACK")

    # First call: ML fails → circuit opens → fallback used
    prob = engine.predict("SKU-FALLBACK", appt, [order])
    # Phase 1 fallback: SKU is on the order → should return 1.0
    assert prob == 1.0
    assert engine.circuit_state == "OPEN"


def test_inference_engine_falls_back_when_not_trained() -> None:
    """When ML model is not trained, engine must fall back silently."""
    from src.scoring.demand_predictor import DemandPredictor

    ml = MLDemandPredictor()  # not trained
    engine = InferenceEngine(ml_predictor=ml, fallback=DemandPredictor())

    appt = _make_appointment()
    order = _make_order(appt, "SKU-1")
    prob = engine.predict("SKU-1", appt, [order])

    # Phase 1 fallback: SKU is on the order → 1.0
    assert prob == 1.0


def test_inference_engine_explain_returns_shap_dict() -> None:
    """explain() must return a dict with FEATURE_NAMES keys when ML is active."""
    engine = _make_trained_engine()
    appt = _make_appointment()
    order = _make_order(appt, "SKU-EXPLAIN")

    shap_vals = engine.explain("SKU-EXPLAIN", appt, [order])

    assert isinstance(shap_vals, dict)
    assert set(shap_vals.keys()) == set(FEATURE_NAMES)


def test_inference_engine_explain_empty_when_not_trained() -> None:
    """explain() must return {} when ML model is not trained."""
    from src.scoring.demand_predictor import DemandPredictor

    ml = MLDemandPredictor()  # not trained
    engine = InferenceEngine(ml_predictor=ml, fallback=DemandPredictor())

    appt = _make_appointment()
    result = engine.explain("SKU-1", appt, [])

    assert result == {}


# ──────────────────────────────────────────────────────────────────────────────
# MovementScorer integration with ML
# ──────────────────────────────────────────────────────────────────────────────

def test_scorer_uses_ml_p_load_when_engine_provided() -> None:
    """When InferenceEngine is provided, scorer should use ML probability."""
    from src.config import ResourceConfig
    from src.models.movements import CandidateMovement
    from src.scoring.value_function import MovementScorer, ScoringContext
    from src.scoring.weights import ScoringWeights

    engine = _make_trained_engine()
    scorer = MovementScorer(
        weights=ScoringWeights(),
        config=ResourceConfig(),
        ml_inference=engine,
    )

    appt = _make_appointment(dock_door=1)
    order = _make_order(appt, "SKU-ML")
    from_loc = Location(
        location_id="FAR", zone="A", aisle=1, bay=1, level=0, x=100.0, y=5.0
    )
    to_loc = Location(
        location_id="STAGE", zone="STAGE", aisle=10, bay=1, level=0,
        x=1.0, y=5.0, is_staging=True, nearest_dock_door=1,
    )
    candidate = CandidateMovement(sku_id="SKU-ML", from_location=from_loc, to_location=to_loc)
    context = ScoringContext(orders=[order], appointments=[appt], resource_utilization=0.2)

    score = scorer.score(candidate, context)

    # Score should be computed (positive or zero depending on ML p_load)
    assert isinstance(score, float)
    assert score >= 0.0


def test_scorer_stores_shap_in_score_components_when_ml_active() -> None:
    """When ML is active and score > 0, score_components should include shap_* keys."""
    from src.config import ResourceConfig
    from src.models.movements import CandidateMovement
    from src.scoring.value_function import MovementScorer, ScoringContext
    from src.scoring.weights import ScoringWeights

    # Use a mock engine that always returns p_load=0.9 and shap values
    mock_engine = MagicMock()
    mock_engine.predict.return_value = 0.9
    mock_engine.explain.return_value = {name: 0.1 for name in FEATURE_NAMES}

    scorer = MovementScorer(
        weights=ScoringWeights(),
        config=ResourceConfig(),
        ml_inference=mock_engine,
    )

    appt = _make_appointment(dock_door=1)
    order = _make_order(appt, "SKU-SHAP")
    from_loc = Location(
        location_id="FAR", zone="A", aisle=1, bay=1, level=0, x=100.0, y=5.0
    )
    to_loc = Location(
        location_id="STAGE", zone="STAGE", aisle=10, bay=1, level=0,
        x=1.0, y=5.0, is_staging=True, nearest_dock_door=1,
    )
    candidate = CandidateMovement(sku_id="SKU-SHAP", from_location=from_loc, to_location=to_loc)
    context = ScoringContext(orders=[order], appointments=[appt], resource_utilization=0.2)

    scorer.score(candidate, context)

    shap_keys = [k for k in candidate.score_components if k.startswith("shap_")]
    assert len(shap_keys) == len(FEATURE_NAMES), (
        f"Expected {len(FEATURE_NAMES)} shap_* keys, got {len(shap_keys)}"
    )


def test_scorer_phase1_path_unchanged_without_ml() -> None:
    """Scorer without InferenceEngine must behave exactly as Phase 1."""
    from src.config import ResourceConfig
    from src.models.movements import CandidateMovement
    from src.scoring.value_function import MovementScorer, ScoringContext
    from src.scoring.weights import ScoringWeights

    scorer = MovementScorer(weights=ScoringWeights(), config=ResourceConfig())

    appt = _make_appointment(dock_door=1)
    order = _make_order(appt, "SKU-P1")
    from_loc = Location(
        location_id="FAR", zone="A", aisle=1, bay=1, level=0, x=100.0, y=5.0
    )
    to_loc = Location(
        location_id="STAGE", zone="STAGE", aisle=10, bay=1, level=0,
        x=1.0, y=5.0, is_staging=True, nearest_dock_door=1,
    )
    candidate = CandidateMovement(sku_id="SKU-P1", from_location=from_loc, to_location=to_loc)
    context = ScoringContext(orders=[order], appointments=[appt], resource_utilization=0.2)

    score = scorer.score(candidate, context)

    assert score > 0.0
    # No shap keys in Phase 1 path
    shap_keys = [k for k in candidate.score_components if k.startswith("shap_")]
    assert len(shap_keys) == 0
