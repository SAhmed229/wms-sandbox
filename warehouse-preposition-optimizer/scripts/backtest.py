#!/usr/bin/env python3
"""Backtest the scorer against historical movement data."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _load_csv(path: str) -> list[dict[str, str]]:
    """Load all rows from a CSV file.

    Args:
        path: Path to CSV file.

    Returns:
        List of row dicts.
    """
    with open(path) as f:
        reader = csv.DictReader(f)
        return list(reader)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Parse a float value safely.

    Args:
        value: Raw value from CSV.
        default: Default if parsing fails.

    Returns:
        Parsed float or default.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compute_correlation(xs: list[float], ys: list[float]) -> float:
    """Compute Pearson correlation coefficient between two lists.

    Args:
        xs: First list of values.
        ys: Second list of values.

    Returns:
        Pearson r in [-1.0, 1.0], or 0.0 if undefined.
    """
    n = len(xs)
    if n < 2:
        return 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n
    std_x = (sum((x - mean_x) ** 2 for x in xs) / n) ** 0.5
    std_y = (sum((y - mean_y) ** 2 for y in ys) / n) ** 0.5

    if std_x == 0 or std_y == 0:
        return 0.0
    return cov / (std_x * std_y)


def _compute_precision_at_k(
    scored: list[dict[str, float]], k: int
) -> float:
    """Compute precision@k: fraction of top-k movements that were actually loaded.

    Args:
        scored: List of dicts with 'score' and 'loaded' (0/1).
        k: Number of top candidates to evaluate.

    Returns:
        Precision@k in [0.0, 1.0].
    """
    if not scored or k == 0:
        return 0.0
    top_k = sorted(scored, key=lambda r: r["score"], reverse=True)[:k]
    loaded_in_top_k = sum(1 for r in top_k if r["loaded"] > 0.5)
    return loaded_in_top_k / len(top_k)


def _run_scorer_on_row(row: dict[str, str]) -> float:
    """Re-compute the value function score from raw CSV fields.

    Expected CSV columns:
        t_saved, p_load, w_order, c_move, c_opportunity

    Args:
        row: CSV row dict.

    Returns:
        Computed V(m) score.
    """
    t_saved = _safe_float(row.get("t_saved"))
    p_load = _safe_float(row.get("p_load"))
    w_order = _safe_float(row.get("w_order"))
    c_move = _safe_float(row.get("c_move"), default=1.0)
    c_opportunity = _safe_float(row.get("c_opportunity"), default=60.0)

    if t_saved <= 0 or p_load == 0:
        return 0.0

    denominator = c_move + c_opportunity
    if denominator <= 0:
        return 0.0

    return (t_saved * p_load * w_order) / denominator


def _print_summary(
    scores: list[float],
    loaded: list[float],
    load_time: list[float],
    k_values: list[int],
) -> None:
    """Print backtest summary statistics to stdout.

    Args:
        scores: Predicted scores.
        loaded: Binary outcome (1=loaded, 0=not loaded).
        load_time: Time to load in seconds for loaded movements.
        k_values: K values for precision@k.

    Returns:
        None
    """
    n = len(scores)
    n_loaded = sum(1 for l in loaded if l > 0.5)

    corr_loaded = _compute_correlation(scores, loaded)
    corr_time = _compute_correlation(
        [s for s, lt in zip(scores, load_time) if lt > 0],
        [lt for lt in load_time if lt > 0],
    )

    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    print(f"  Total movements evaluated:  {n}")
    print(f"  Movements actually loaded:  {n_loaded} ({n_loaded/n*100:.1f}%)")
    print(f"  Score range:                [{min(scores):.3f}, {max(scores):.3f}]")
    print(f"  Mean score:                 {sum(scores)/n:.3f}")
    print()
    print(f"  Correlation (score vs loaded):     r = {corr_loaded:.3f}")
    print(f"  Correlation (score vs load_time):  r = {corr_time:.3f}")
    print()

    scored_rows = [
        {"score": s, "loaded": l} for s, l in zip(scores, loaded)
    ]
    for k in k_values:
        pk = _compute_precision_at_k(scored_rows, k)
        print(f"  Precision@{k:3d}:  {pk:.3f}")
    print("=" * 60)


def main() -> int:
    """Entry point for backtest CLI.

    Returns:
        Exit code (0 = success).
    """
    parser = argparse.ArgumentParser(
        description="Backtest the scoring function against historical warehouse movements.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Expected CSV columns (historical movements):
  sku_id, order_id, from_location, to_location,
  t_saved, p_load, w_order, c_move, c_opportunity,
  loaded (0 or 1), load_time_seconds

Examples:
  python backtest.py --csv data/historical_movements.csv
  python backtest.py --csv data/movements.csv --date-range 2024-01-01:2024-03-31
        """,
    )
    parser.add_argument(
        "--csv",
        type=str,
        required=True,
        help="Path to historical movements CSV file.",
    )
    parser.add_argument(
        "--date-range",
        type=str,
        default=None,
        help="Date range filter in format YYYY-MM-DD:YYYY-MM-DD.",
    )
    parser.add_argument(
        "--top-k",
        type=str,
        default="5,10,20,50",
        help="Comma-separated K values for precision@k (default: 5,10,20,50).",
    )
    args = parser.parse_args()

    csv_path = args.csv
    if not Path(csv_path).exists():
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        return 1

    # Parse date range
    start_date: datetime | None = None
    end_date: datetime | None = None
    if args.date_range:
        try:
            parts = args.date_range.split(":")
            start_date = datetime.fromisoformat(parts[0]).replace(tzinfo=UTC)
            end_date = datetime.fromisoformat(parts[1]).replace(tzinfo=UTC)
            print(f"Filtering to date range: {start_date.date()} to {end_date.date()}")
        except (ValueError, IndexError) as exc:
            print(f"Error parsing date range: {exc}", file=sys.stderr)
            return 1

    # Parse K values
    try:
        k_values = [int(k) for k in args.top_k.split(",")]
    except ValueError:
        k_values = [5, 10, 20, 50]

    # Load and filter rows
    print(f"Loading data from: {csv_path}")
    rows = _load_csv(csv_path)
    print(f"  Total rows loaded: {len(rows)}")

    if start_date and end_date:
        filtered: list[dict[str, str]] = []
        for row in rows:
            date_str = row.get("date", row.get("dispatched_at", ""))
            try:
                row_date = datetime.fromisoformat(date_str).replace(tzinfo=UTC)
                if start_date <= row_date <= end_date:
                    filtered.append(row)
            except (ValueError, TypeError):
                filtered.append(row)  # Include rows with no date
        rows = filtered
        print(f"  Rows after date filter: {len(rows)}")

    if not rows:
        print("No rows to evaluate after filtering.", file=sys.stderr)
        return 1

    # Score each row
    scores: list[float] = []
    loaded: list[float] = []
    load_times: list[float] = []

    for row in rows:
        score = _run_scorer_on_row(row)
        scores.append(score)
        loaded.append(_safe_float(row.get("loaded", "0")))
        load_times.append(_safe_float(row.get("load_time_seconds", "0")))

    _print_summary(scores, loaded, load_times, k_values)
    return 0


if __name__ == "__main__":
    sys.exit(main())
