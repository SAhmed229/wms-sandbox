#!/usr/bin/env python
"""Train the Phase 2 LightGBM demand predictor and validate against AUC gate.

This is the canonical activation path for Phase 2 ML demand prediction.
For real customers it runs against their exported historical loading data;
for development it runs against `scripts/generate_training_data.py --synthetic`.

Usage
-----
Default (50k synthetic rows + sane defaults)::

    uv run python scripts/train_phase2.py \\
        --training-csv data/training.csv \\
        --out models/demand_lgbm.pkl

Strict AUC gate (fail CI if model isn't shippable)::

    uv run python scripts/train_phase2.py \\
        --training-csv data/training.csv \\
        --out models/demand_lgbm.pkl \\
        --min-auc 0.80
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    """Train model, report validation metrics, exit 1 if AUC gate fails.

    Returns:
        Exit code (0 = passed gate, 1 = below gate or error).
    """
    parser = argparse.ArgumentParser(
        description="Train and validate the Phase 2 LightGBM demand predictor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--training-csv",
        type=str,
        required=True,
        help="Path to training data CSV (output of generate_training_data.py).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="models/demand_lgbm.pkl",
        help="Where to save the trained model artifact.",
    )
    parser.add_argument(
        "--min-auc",
        type=float,
        default=0.70,
        help=(
            "Minimum cv_auc_mean required to pass the validation gate. "
            "Default 0.70 is the SYNTHETIC-DATA gate (the generator in "
            "scripts/generate_training_data.py injects Gaussian noise that "
            "caps achievable AUC at ~0.72). For production runs against "
            "real customer history, raise this to 0.75 per HUMAN_TODO #19."
        ),
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=50,
        help="Number of Optuna hyperparameter search trials.",
    )
    parser.add_argument(
        "--cv-folds",
        type=int,
        default=5,
        help="Number of TimeSeriesSplit CV folds.",
    )
    args = parser.parse_args()

    csv_path = Path(args.training_csv)
    if not csv_path.exists():
        print(f"ERROR: training CSV not found: {csv_path}", file=sys.stderr)
        return 1

    # Lazy imports so --help doesn't pay the cost
    import numpy as np
    import pandas as pd
    from sklearn.metrics import brier_score_loss

    from src.prediction.features import FEATURE_NAMES
    from src.prediction.trainer import MLDemandPredictor

    print(f"Loading {csv_path} ...", file=sys.stderr)
    df = pd.read_csv(csv_path)
    print(f"  rows={len(df)}  positive_rate={df['was_loaded'].mean():.3f}", file=sys.stderr)

    missing = [c for c in FEATURE_NAMES if c not in df.columns]
    if missing:
        print(f"ERROR: training CSV missing feature columns: {missing}", file=sys.stderr)
        return 1
    if "was_loaded" not in df.columns:
        print("ERROR: training CSV missing 'was_loaded' target column.", file=sys.stderr)
        return 1

    predictor = MLDemandPredictor()
    print(
        f"Training (n_trials={args.n_trials}, cv_folds={args.cv_folds}) ...",
        file=sys.stderr,
    )
    metrics = predictor.train(
        training_data=df,
        target_col="was_loaded",
        n_trials=args.n_trials,
        cv_folds=args.cv_folds,
    )

    # Brier score (calibration quality) on full data — uses the calibrated model.
    X = df[FEATURE_NAMES].values.astype(np.float32)
    y = df["was_loaded"].values.astype(np.int32)
    proba = predictor._calibrated.predict_proba(X)[:, 1]  # noqa: SLF001
    brier = brier_score_loss(y, proba)

    # SHAP feature importance: mean |SHAP value| per feature on a 2000-row sample.
    sample = df.sample(min(2000, len(df)), random_state=7)[FEATURE_NAMES].values.astype(np.float32)
    shap_vals = predictor._explainer.shap_values(sample)  # noqa: SLF001
    if isinstance(shap_vals, list):
        shap_vals = shap_vals[1] if len(shap_vals) > 1 else shap_vals[0]
    shap_vals = np.asarray(shap_vals)
    if shap_vals.ndim == 3:
        shap_vals = shap_vals[..., 1] if shap_vals.shape[-1] > 1 else shap_vals[..., 0]
    importances = np.abs(shap_vals).mean(axis=0)
    ranked = sorted(zip(FEATURE_NAMES, importances), key=lambda kv: kv[1], reverse=True)

    print()
    print("=" * 60)
    print("Phase 2 Validation Report")
    print("=" * 60)
    print(f"  CV AUC (mean):   {metrics['cv_auc_mean']:.4f}")
    print(f"  CV AUC (std):    {metrics['cv_auc_std']:.4f}")
    print(f"  Brier score:     {brier:.4f}  (lower is better; <0.25 is well calibrated)")
    print()
    print("  Top-10 features by mean |SHAP|:")
    for name, imp in ranked[:10]:
        bar = "█" * int(imp * 200 / max(ranked[0][1], 1e-9))
        print(f"    {name:35s} {imp:.4f}  {bar}")
    print()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    predictor.save(out_path)
    print(f"Model saved to {out_path}", file=sys.stderr)

    auc = metrics["cv_auc_mean"]
    if auc < args.min_auc:
        print(
            f"FAIL: CV AUC {auc:.4f} below gate of {args.min_auc:.2f}.",
            file=sys.stderr,
        )
        return 1
    print(
        f"PASS: CV AUC {auc:.4f} >= gate of {args.min_auc:.2f}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
