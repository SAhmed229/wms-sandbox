#!/usr/bin/env python3
"""Calibrate scoring weights using Analytic Hierarchy Process (AHP)."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

import yaml

# Weight dimensions in the value function
CRITERIA = [
    "time_saved",
    "load_probability",
    "order_priority",
    "movement_cost",
    "opportunity_cost",
]

# AHP scale: 1=equal, 3=moderate, 5=strong, 7=very strong, 9=extreme
AHP_SCALE_LABELS = {
    1: "Equal importance",
    2: "Weak preference",
    3: "Moderate importance",
    4: "Moderate-strong",
    5: "Strong importance",
    6: "Strong-very strong",
    7: "Very strong importance",
    8: "Very-extreme",
    9: "Extreme importance",
}

# Random index for AHP consistency check (Saaty, n=1..10)
_RANDOM_INDEX = {
    1: 0.0,
    2: 0.0,
    3: 0.58,
    4: 0.90,
    5: 1.12,
    6: 1.24,
    7: 1.32,
    8: 1.41,
    9: 1.45,
    10: 1.49,
}


def _build_comparison_matrix(n: int) -> list[list[float]]:
    """Build an n×n identity comparison matrix.

    Args:
        n: Number of criteria.

    Returns:
        n×n identity matrix as list of lists.
    """
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _prompt_pairwise(criteria: list[str]) -> list[list[float]]:
    """Interactively prompt user to fill in pairwise comparison matrix.

    Args:
        criteria: List of criterion names.

    Returns:
        Filled n×n comparison matrix.
    """
    n = len(criteria)
    matrix = _build_comparison_matrix(n)

    print("\nPairwise Comparison Matrix Entry")
    print("=" * 60)
    print("Rate the relative importance of criterion A vs criterion B.")
    print("Enter a value from 1 (equal) to 9 (A is extremely more important).")
    print("Enter 1/x (e.g. 1/3) if B is more important than A.\n")

    for i in range(n):
        for j in range(i + 1, n):
            crit_a = criteria[i]
            crit_b = criteria[j]
            while True:
                raw = input(f"  {crit_a} vs {crit_b}: ").strip()
                try:
                    if "/" in raw:
                        parts = raw.split("/")
                        value = float(parts[0]) / float(parts[1])
                    else:
                        value = float(raw)
                    if value <= 0:
                        print("  Value must be positive.")
                        continue
                    matrix[i][j] = value
                    matrix[j][i] = 1.0 / value
                    break
                except (ValueError, ZeroDivisionError):
                    print(f"  Invalid input '{raw}'. Enter a number like 3 or 1/3.")

    return matrix


def _compute_ahp_weights(matrix: list[list[float]]) -> tuple[list[float], float]:
    """Compute normalized AHP weights and consistency ratio.

    Args:
        matrix: n×n pairwise comparison matrix.

    Returns:
        Tuple of (weights list, consistency_ratio).
    """
    n = len(matrix)

    # Column sums
    col_sums = [sum(matrix[r][c] for r in range(n)) for c in range(n)]

    # Normalize matrix
    normalized = [
        [matrix[r][c] / col_sums[c] for c in range(n)] for r in range(n)
    ]

    # Priority vector (row averages of normalized matrix)
    weights = [sum(normalized[r]) / n for r in range(n)]

    # Consistency check: compute lambda_max
    weighted_sums = [
        sum(matrix[r][c] * weights[c] for c in range(n)) for r in range(n)
    ]
    lambda_max = sum(weighted_sums[r] / weights[r] for r in range(n)) / n

    ci = (lambda_max - n) / (n - 1) if n > 1 else 0.0
    ri = _RANDOM_INDEX.get(n, 1.49)
    cr = ci / ri if ri > 0 else 0.0

    return weights, cr


def _load_csv_and_compute_weights(csv_path: str) -> list[float]:
    """Load historical movement data from CSV and compute weights via regression proxy.

    Uses simple correlation of each feature with a binary "loaded" outcome.

    Args:
        csv_path: Path to CSV file with columns: time_saved, load_probability,
                  order_priority, movement_cost, opportunity_cost, loaded (0/1).

    Returns:
        Normalized weight list for each criterion.
    """
    rows: list[dict[str, float]] = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({k: float(v) for k, v in row.items()})
            except (ValueError, KeyError):
                continue

    if not rows:
        print(f"Warning: No valid rows found in {csv_path}. Using equal weights.")
        return [1.0 / len(CRITERIA)] * len(CRITERIA)

    # Simple correlation-based weight estimation
    loaded = [r.get("loaded", 0.0) for r in rows]
    correlations: list[float] = []

    for criterion in CRITERIA:
        values = [r.get(criterion, 0.0) for r in rows]
        mean_v = sum(values) / len(values) if values else 0.0
        mean_l = sum(loaded) / len(loaded) if loaded else 0.0

        cov = sum(
            (v - mean_v) * (l - mean_l) for v, l in zip(values, loaded)
        ) / len(values)
        std_v = (sum((v - mean_v) ** 2 for v in values) / len(values)) ** 0.5
        std_l = (sum((l - mean_l) ** 2 for l in loaded) / len(loaded)) ** 0.5

        if std_v > 0 and std_l > 0:
            corr = abs(cov / (std_v * std_l))
        else:
            corr = 0.0
        correlations.append(corr)

    total = sum(correlations) or 1.0
    return [c / total for c in correlations]


def _format_weights_yaml(weights: list[float], criteria: list[str]) -> str:
    """Format calibrated weights as YAML string.

    Args:
        weights: Weight values.
        criteria: Criterion names.

    Returns:
        YAML string with scoring.weights section.
    """
    weight_dict = dict(zip(criteria, [round(w, 4) for w in weights]))
    return yaml.dump({"scoring": {"weights": weight_dict}}, default_flow_style=False)


def main() -> int:
    """Entry point for weight calibration CLI.

    Returns:
        Exit code (0 = success).
    """
    parser = argparse.ArgumentParser(
        description="Calibrate scoring weights for the warehouse pre-positioning optimizer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python calibrate_weights.py --interactive
  python calibrate_weights.py --csv data/movements.csv
  python calibrate_weights.py --interactive --output config_calibrated.yml
        """,
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for pairwise AHP comparisons interactively.",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Path to historical movements CSV for correlation-based calibration.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output YAML file path. Prints to stdout if not specified.",
    )
    args = parser.parse_args()

    if not args.interactive and not args.csv:
        print("Error: Specify --interactive or --csv <file>.", file=sys.stderr)
        parser.print_help()
        return 1

    weights: list[float]
    cr: float = 0.0

    if args.interactive:
        print("Analytic Hierarchy Process (AHP) Weight Calibration")
        print("=" * 60)
        print(f"Criteria: {', '.join(CRITERIA)}\n")
        matrix = _prompt_pairwise(CRITERIA)
        weights, cr = _compute_ahp_weights(matrix)
        if cr > 0.1:
            print(f"\nWarning: Consistency Ratio = {cr:.3f} (> 0.10, consider revising judgements)")
        else:
            print(f"\nConsistency Ratio = {cr:.3f} (acceptable)")
    elif args.csv:
        csv_path = args.csv
        if not Path(csv_path).exists():
            print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
            return 1
        weights = _load_csv_and_compute_weights(csv_path)
    else:
        weights = [1.0 / len(CRITERIA)] * len(CRITERIA)

    output_yaml = _format_weights_yaml(weights, CRITERIA)

    print("\nCalibrated Weights:")
    print("-" * 40)
    for crit, w in zip(CRITERIA, weights):
        print(f"  {crit}: {w:.4f}")

    print("\nYAML Output:")
    print(output_yaml)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_yaml)
        print(f"Written to: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
