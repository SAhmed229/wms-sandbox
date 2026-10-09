"""Generate synthetic training data for Phase 2 ML demand prediction.

This script produces a labelled dataset of (SKU, dock_door, time_window, features,
was_loaded) rows. Run it against a real WMS database export for actual training, or
use the --synthetic flag to generate realistic synthetic data for development.

Usage:
    # Synthetic data for dev/testing:
    uv run python scripts/generate_training_data.py --synthetic --rows 10000 --out data/training.csv

    # From real WMS database:
    uv run python scripts/generate_training_data.py \\
        --db-url postgresql+psycopg2://wms:wms@localhost:5432/wms \\
        --start-date 2024-01-01 --end-date 2024-03-31 \\
        --out data/training.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _generate_synthetic(n_rows: int, seed: int = 42) -> "pd.DataFrame":
    """Generate synthetic training data with realistic correlations.

    Correlations baked in:
    - ABC class A SKUs load more often than C SKUs
    - Orders on high-priority orders load more often
    - SKUs close to cutoff load more often
    - High carrier_sku_frequency → higher load probability

    Args:
        n_rows: Number of rows to generate.
        seed: Random seed for reproducibility.

    Returns:
        DataFrame with FEATURE_NAMES columns plus was_loaded target.
    """
    import math

    import numpy as np
    import pandas as pd

    from src.prediction.features import FEATURE_NAMES

    rng = np.random.default_rng(seed)

    rows = []
    for i in range(n_rows):
        # Reference time: spread over 90 days
        day_offset = rng.integers(0, 90)
        hour = rng.integers(0, 24)
        ref = datetime(2024, 1, 1, int(hour), 0, tzinfo=UTC) + timedelta(days=int(day_offset))

        hour_f = ref.hour + ref.minute / 60.0
        dow = ref.weekday()

        abc_ordinal = float(rng.choice([1.0, 2.0, 3.0], p=[0.5, 0.3, 0.2]))
        avg_demand = rng.exponential(scale=50.0)
        demand_cv = rng.uniform(0.1, 1.5)
        days_since = float(rng.integers(1, 60))
        on_hand = float(rng.integers(0, 200))
        pending_qty = float(rng.integers(0, 50))

        carrier_enc = float(rng.integers(0, 20))
        carrier_freq = rng.uniform(0.0, 0.8)
        appt_duration = float(rng.integers(60, 240))
        dock_zone_match = float(rng.choice([0, 1], p=[0.7, 0.3]))

        order_exists = float(rng.choice([0, 1], p=[0.4, 0.6]))
        order_priority = float(rng.integers(1, 11)) if order_exists else 0.0
        minutes_cutoff = float(rng.integers(-30, 480)) if order_exists else 0.0
        fill_rate = rng.uniform(0.0, 1.0)

        days_in_month = 31  # simplified
        days_until_eom = float(days_in_month - (ref.day % days_in_month))

        features = {
            "hour_of_day_sin": math.sin(2 * math.pi * hour_f / 24.0),
            "hour_of_day_cos": math.cos(2 * math.pi * hour_f / 24.0),
            "day_of_week_sin": math.sin(2 * math.pi * dow / 7.0),
            "day_of_week_cos": math.cos(2 * math.pi * dow / 7.0),
            "days_until_month_end": days_until_eom,
            "is_holiday": 0.0,
            "abc_class_ordinal": abc_ordinal,
            "avg_daily_demand_30d": avg_demand,
            "demand_cv_30d": demand_cv,
            "days_since_last_shipment": days_since,
            "current_on_hand_quantity": on_hand,
            "pending_order_quantity": pending_qty,
            "carrier_id_encoded": carrier_enc,
            "carrier_sku_frequency": carrier_freq,
            "appointment_duration_minutes": appt_duration,
            "dock_zone_match": dock_zone_match,
            "order_exists_for_sku": order_exists,
            "order_priority": order_priority,
            "minutes_until_cutoff": minutes_cutoff,
            "order_fill_rate": fill_rate,
        }

        # Realistic label generation: load probability driven by key signals
        log_odds = (
            -1.5
            + 0.5 * (abc_ordinal - 1.0)      # A class more likely to load
            + 2.0 * order_exists               # Order existence is strong signal
            + 0.02 * order_priority            # Higher priority = more likely
            - 0.003 * max(minutes_cutoff, 0)   # Approaching cutoff = more urgent
            + 0.5 * carrier_freq               # Frequent carrier/SKU = more likely
            + 0.3 * dock_zone_match            # Nearby = more likely
            + rng.normal(0, 0.5)               # Noise
        )
        p_load = 1.0 / (1.0 + math.exp(-log_odds))
        was_loaded = int(rng.random() < p_load)

        rows.append({**features, "was_loaded": was_loaded})

    df = pd.DataFrame(rows)
    assert list(df.columns[:-1]) == FEATURE_NAMES, "Feature column mismatch"
    return df


def _generate_from_db(
    db_url: str, start_date: str, end_date: str
) -> "pd.DataFrame":
    """Extract and label training data from a WMS database export.

    Expected tables: inventory_positions, outbound_orders, order_lines,
    carrier_appointments, locations, skus.

    Args:
        db_url: SQLAlchemy connection string (sync driver).
        start_date: ISO date string for data window start.
        end_date: ISO date string for data window end.

    Returns:
        DataFrame with FEATURE_NAMES columns plus was_loaded target.
    """
    try:
        import pandas as pd
        import sqlalchemy as sa
    except ImportError:
        print("pandas and sqlalchemy required for DB extraction.", file=sys.stderr)
        sys.exit(1)

    engine = sa.create_engine(db_url)

    query = f"""
        SELECT
            ol.sku_id,
            ca.dock_door,
            ca.scheduled_arrival AS window_start,
            ca.scheduled_departure AS window_end,
            ca.carrier,
            -- Label: was this SKU actually loaded on this appointment?
            CASE WHEN ol.picked = TRUE THEN 1 ELSE 0 END AS was_loaded,
            -- Raw signals for feature engineering
            s.abc_class,
            ip.quantity AS on_hand_quantity,
            oo.priority AS order_priority,
            EXTRACT(EPOCH FROM (ca.scheduled_arrival - NOW())) / 60 AS minutes_until_arrival
        FROM outbound_orders oo
        JOIN carrier_appointments ca USING (appointment_id)
        JOIN order_lines ol USING (order_id)
        JOIN skus s ON s.sku_id = ol.sku_id
        LEFT JOIN inventory_positions ip ON ip.sku_id = ol.sku_id
        WHERE ca.scheduled_arrival BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY ca.scheduled_arrival
    """

    with engine.connect() as conn:
        df = pd.read_sql(query, conn)

    print(f"Extracted {len(df):,} rows from database.", file=sys.stderr)
    # TODO: run FeatureBuilder over each row to produce full feature vectors.
    # For now return the raw extract; a follow-up script applies FeatureBuilder.
    return df


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate training data for Phase 2 ML demand prediction."
    )
    parser.add_argument("--synthetic", action="store_true", help="Generate synthetic data.")
    parser.add_argument("--rows", type=int, default=10_000, help="Rows to generate (synthetic).")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (synthetic).")
    parser.add_argument("--db-url", type=str, help="SQLAlchemy DB URL (real data).")
    parser.add_argument("--start-date", type=str, help="Start date YYYY-MM-DD (real data).")
    parser.add_argument("--end-date", type=str, help="End date YYYY-MM-DD (real data).")
    parser.add_argument("--out", type=str, default="data/training.csv", help="Output CSV path.")
    args = parser.parse_args()

    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        print("pandas is required. Run: uv add pandas", file=sys.stderr)
        sys.exit(1)

    if args.synthetic:
        print(f"Generating {args.rows:,} synthetic rows (seed={args.seed})...", file=sys.stderr)
        df = _generate_synthetic(n_rows=args.rows, seed=args.seed)
    elif args.db_url:
        if not args.start_date or not args.end_date:
            parser.error("--start-date and --end-date required with --db-url")
        print(f"Extracting from database: {args.start_date} → {args.end_date}", file=sys.stderr)
        df = _generate_from_db(args.db_url, args.start_date, args.end_date)
    else:
        parser.error("Either --synthetic or --db-url must be specified.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    label_rate = df["was_loaded"].mean() if "was_loaded" in df.columns else float("nan")
    print(
        f"Wrote {len(df):,} rows to {out_path}  "
        f"(positive rate: {label_rate:.1%})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
