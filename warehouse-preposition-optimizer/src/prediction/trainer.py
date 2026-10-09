"""LightGBM demand predictor: training, prediction, and SHAP explanation (Phase 2)."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import optuna
import shap
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

from src.prediction.features import FEATURE_NAMES

optuna.logging.set_verbosity(optuna.logging.WARNING)


class MLDemandPredictor:
    """LightGBM binary classifier predicting P(SKU loaded at appointment).

    Trained on (SKU, dock_door, 2-hour time window) triples labelled
    with was_loaded ∈ {0, 1}. Supports SHAP-based explanation.

    Public interface:
        train(training_data, target_col, n_trials, cv_folds)
        predict(features) -> float
        explain(features) -> dict[str, float]
        save(path)
        load(path)
    """

    def __init__(self) -> None:
        self._model: lgb.LGBMClassifier | None = None
        self._calibrated: CalibratedClassifierCV | None = None
        self._explainer: shap.TreeExplainer | None = None
        self._is_trained: bool = False

    # ──────────────────────────────────────────────────────────────────────────
    # Training
    # ──────────────────────────────────────────────────────────────────────────

    def train(
        self,
        training_data: Any,  # pandas DataFrame
        target_col: str = "was_loaded",
        n_trials: int = 50,
        cv_folds: int = 5,
    ) -> dict[str, float]:
        """Train LightGBM with Optuna hyperparameter search and isotonic calibration.

        Uses TimeSeriesSplit cross-validation to avoid data leakage.
        Class imbalance is handled via scale_pos_weight.

        Args:
            training_data: DataFrame with feature columns matching FEATURE_NAMES
                plus the target column.
            target_col: Binary target column name (1 = was_loaded).
            n_trials: Number of Optuna hyperparameter search trials.
            cv_folds: Number of TimeSeriesSplit folds.

        Returns:
            Dict with cv_auc_mean, cv_auc_std, and best_params.
        """
        import pandas as pd
        from sklearn.metrics import roc_auc_score

        df: pd.DataFrame = training_data
        X = df[FEATURE_NAMES].values.astype(np.float32)
        y = df[target_col].values.astype(np.int32)

        # Class imbalance weight
        n_pos = int(y.sum())
        n_neg = len(y) - n_pos
        scale_pos_weight = n_neg / max(n_pos, 1)

        tscv = TimeSeriesSplit(n_splits=cv_folds)

        def _objective(trial: optuna.Trial) -> float:
            params = {
                "num_leaves": trial.suggest_int("num_leaves", 15, 63),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
                "min_child_samples": trial.suggest_int("min_child_samples", 5, 50),
                "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
                "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 10.0),
                "reg_lambda": trial.suggest_float("reg_lambda", 0.0, 10.0),
                "scale_pos_weight": scale_pos_weight,
                "n_estimators": 200,
                "random_state": 42,
                "verbose": -1,
            }
            aucs: list[float] = []
            for train_idx, val_idx in tscv.split(X):
                clf = lgb.LGBMClassifier(**params)
                clf.fit(X[train_idx], y[train_idx])
                proba = clf.predict_proba(X[val_idx])[:, 1]
                if len(np.unique(y[val_idx])) < 2:
                    continue
                aucs.append(float(roc_auc_score(y[val_idx], proba)))
            return float(np.mean(aucs)) if aucs else 0.0

        study = optuna.create_study(direction="maximize")
        study.optimize(_objective, n_trials=n_trials, show_progress_bar=False)

        best_params: dict[str, Any] = {
            **study.best_params,
            "scale_pos_weight": scale_pos_weight,
            "n_estimators": 200,
            "random_state": 42,
            "verbose": -1,
        }

        # Train final model on all data with best params
        base_model = lgb.LGBMClassifier(**best_params)

        # Calibrate with isotonic regression for reliable probabilities
        self._calibrated = CalibratedClassifierCV(base_model, method="isotonic", cv=3)
        self._calibrated.fit(X, y)

        # Keep a plain LGBMClassifier for SHAP (CalibratedClassifier wraps it)
        self._model = lgb.LGBMClassifier(**best_params)
        self._model.fit(X, y)
        self._explainer = shap.TreeExplainer(self._model)
        self._is_trained = True

        # Final CV AUC
        cv_aucs: list[float] = []
        for train_idx, val_idx in tscv.split(X):
            clf = lgb.LGBMClassifier(**best_params)
            clf.fit(X[train_idx], y[train_idx])
            proba = clf.predict_proba(X[val_idx])[:, 1]
            if len(np.unique(y[val_idx])) < 2:
                continue
            cv_aucs.append(float(roc_auc_score(y[val_idx], proba)))

        return {
            "cv_auc_mean": float(np.mean(cv_aucs)) if cv_aucs else 0.0,
            "cv_auc_std": float(np.std(cv_aucs)) if cv_aucs else 0.0,
            "best_params": best_params,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Inference
    # ──────────────────────────────────────────────────────────────────────────

    def predict(self, features: dict[str, float]) -> float:
        """Return calibrated P(was_loaded) for a single feature dict.

        Args:
            features: Dict with keys matching FEATURE_NAMES.

        Returns:
            Calibrated probability in [0.0, 1.0].

        Raises:
            RuntimeError: If called before train() or load().
        """
        if self._calibrated is None:
            raise RuntimeError("Model not trained. Call train() or load() first.")
        x = _dict_to_array(features)
        proba: float = float(self._calibrated.predict_proba(x)[0, 1])
        return max(0.0, min(1.0, proba))

    def explain(self, features: dict[str, float]) -> dict[str, float]:
        """Return SHAP values for each feature in a single prediction.

        Args:
            features: Dict with keys matching FEATURE_NAMES.

        Returns:
            Dict mapping feature name → SHAP value (contribution to log-odds).

        Raises:
            RuntimeError: If called before train() or load().
        """
        if self._explainer is None or self._model is None:
            raise RuntimeError("Model not trained. Call train() or load() first.")
        x = _dict_to_array(features)
        shap_values = self._explainer.shap_values(x)
        # For binary classification, shap_values may be a list [neg, pos] or 3D array.
        if isinstance(shap_values, list):
            # SHAP returns [shap_neg, shap_pos] for binary classifiers.
            values = shap_values[1][0]
        else:
            # Newer SHAP returns a 3D array (samples, features, classes) or 2D.
            arr = np.array(shap_values)
            if arr.ndim == 3:
                values = arr[0, :, 1]
            else:
                values = arr[0]
        return {name: float(v) for name, v in zip(FEATURE_NAMES, values)}

    # ──────────────────────────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """Persist model and explainer to disk.

        Args:
            path: File path to write the pickle artifact.
        """
        if not self._is_trained:
            raise RuntimeError("Cannot save untrained model.")
        payload = {
            "calibrated": self._calibrated,
            "model": self._model,
            "explainer": self._explainer,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(payload, f)

    def load(self, path: str | Path) -> None:
        """Load model and explainer from disk.

        Args:
            path: File path to read the pickle artifact.

        Raises:
            FileNotFoundError: If the model file does not exist.
        """
        with open(path, "rb") as f:
            payload = pickle.load(f)  # noqa: S301
        self._calibrated = payload["calibrated"]
        self._model = payload["model"]
        self._explainer = payload["explainer"]
        self._is_trained = True

    @property
    def is_trained(self) -> bool:
        """True if the model is ready for inference."""
        return self._is_trained


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _dict_to_array(features: dict[str, float]) -> np.ndarray:
    """Convert feature dict to (1, n_features) array in canonical order.

    Args:
        features: Feature dict with FEATURE_NAMES keys.

    Returns:
        float32 array with shape (1, len(FEATURE_NAMES)).
    """
    return np.array(
        [[features.get(name, 0.0) for name in FEATURE_NAMES]],
        dtype=np.float32,
    )
