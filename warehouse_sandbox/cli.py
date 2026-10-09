"""Replay an authorized export without starting the browser server."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import structlog

from .importer import ValidationError, import_csv, normalize_json_import, normalize_settings


def main():
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    parser = argparse.ArgumentParser(description="Compare historical warehouse data with simulated adoption of suggestions.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Canonical dataset JSON file")
    source.add_argument("--csv-dir", type=Path, help="Folder of canonical CSV exports")
    parser.add_argument("--settings", type=Path, help="Optional scenario settings JSON")
    parser.add_argument("--output", type=Path, default=Path("runs/cli-result.json"))
    parser.add_argument("--facility-name", default="Imported warehouse")
    parser.add_argument("--start-time", default="2026-09-14T06:00:00-04:00")
    args = parser.parse_args()
    try:
        restored_settings = None
        if args.input:
            dataset, restored_settings = normalize_json_import(args.input.read_text())
        else:
            files = {file.name: file.read_text() for file in args.csv_dir.glob("*.csv")}
            dataset = import_csv(files, args.facility_name, args.start_time)
        settings = normalize_settings(json.loads(args.settings.read_text(encoding="utf-8-sig")) if args.settings else restored_settings, dataset)
        from .engine import run_comparison
        result = run_comparison(dataset, settings)
        result["dataset"] = dataset
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        baseline, proposed = result["baseline"]["metrics"], result["proposed"]["metrics"]
        print(dataset["facility"]["name"])
        print(f"Average truck dwell: {baseline['avg_dwell_seconds']:.1f}s baseline → {proposed['avg_dwell_seconds']:.1f}s proposed")
        print(f"Late trucks: {baseline['late_trucks']} baseline → {proposed['late_trucks']} proposed")
        print(f"Suggestions: {len(result['proposed']['suggestions'])}")
        print(f"Model estimate saved to {args.output.resolve()}")
    except (ValidationError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
