#!/usr/bin/env python
"""Import warehouse data from a directory of canonical CSV files.

Usage
-----
List the expected CSV files and their columns::

    uv run python scripts/import_csv.py --help-format

Import to stdout (review before applying)::

    uv run python scripts/import_csv.py scripts/sample_csv/

Save to a file::

    uv run python scripts/import_csv.py scripts/sample_csv/ --out out.sql

Apply directly to the configured database::

    uv run python scripts/import_csv.py scripts/sample_csv/ --apply \\
        --database-url "postgresql://wms:wms@localhost:5433/wms"

Replace all existing data with a fresh import::

    uv run python scripts/import_csv.py scripts/sample_csv/ --apply --truncate

Use a customer-renamed schema (column/table names differ from init_db.sql)::

    uv run python scripts/import_csv.py /data/customer_csvs/ \\
        --schema-path wms_schema.yml --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.ingestion.csv_loader import (
    CSVImportError,
    TABLE_SPECS,
    apply_sql,
    generate_sql,
    load_csv_dir,
)
from src.ingestion.schema import load_wms_schema


def _print_format_help() -> None:
    """Print the expected canonical CSV format for each table."""
    print("Canonical CSV format")
    print("=" * 70)
    print(
        "Drop one CSV per table into the import directory. Headers must match\n"
        "the canonical column names below. Missing optional columns are OK;\n"
        "missing required columns abort the import.\n"
    )
    for spec in TABLE_SPECS:
        print(f"  {spec.csv_filename}")
        for col in spec.columns:
            marker = "required" if col.required else "optional"
            print(f"    {col.name:30s}  {col.sql_type:8s}  ({marker})")
        print()


def main() -> int:
    """Entry point for CSV import CLI.

    Returns:
        Exit code (0 = success, 1 = bad args / parse error, 2 = warnings only).
    """
    parser = argparse.ArgumentParser(
        description="Import canonical CSVs into the WMS schema "
        "(remapped via wms_schema.yml when present).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "csv_dir",
        type=str,
        nargs="?",
        help="Directory containing canonical CSV files.",
    )
    parser.add_argument(
        "--help-format",
        action="store_true",
        help="Print the expected canonical CSV format and exit.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Write generated SQL to this file instead of stdout.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the generated SQL against --database-url (default: $DATABASE_URL).",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Override DATABASE_URL environment variable when --apply is set.",
    )
    parser.add_argument(
        "--schema-path",
        type=str,
        default=None,
        help="Path to wms_schema.yml for customer-renamed table/column names.",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="TRUNCATE all target tables before inserting (FK-safe order).",
    )
    parser.add_argument(
        "--no-transaction",
        action="store_true",
        help="Skip BEGIN/COMMIT wrapping (default wraps the script).",
    )
    args = parser.parse_args()

    if args.help_format:
        _print_format_help()
        return 0

    if not args.csv_dir:
        parser.print_help()
        return 1

    schema = load_wms_schema(args.schema_path) if args.schema_path else load_wms_schema(None)

    try:
        parsed = load_csv_dir(args.csv_dir)
    except CSVImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not parsed:
        print(f"No canonical CSV files found in {args.csv_dir}", file=sys.stderr)
        print("Run with --help-format to see expected filenames.", file=sys.stderr)
        return 1

    print(f"Parsed {args.csv_dir}:", file=sys.stderr)
    for canonical, result in parsed.items():
        print(f"  {canonical:25s}  {len(result.rows):5d} rows", file=sys.stderr)
        for w in result.warnings:
            print(f"    ⚠  {w}", file=sys.stderr)

    sql = generate_sql(
        parsed,
        schema=schema,
        truncate=args.truncate,
        wrap_in_transaction=not args.no_transaction,
    )

    if args.out:
        Path(args.out).write_text(sql, encoding="utf-8")
        print(f"SQL written to {args.out}", file=sys.stderr)
    elif not args.apply:
        print(sql)

    if args.apply:
        db_url = args.database_url or os.environ.get("DATABASE_URL")
        if not db_url:
            print(
                "ERROR: --apply requires --database-url or DATABASE_URL env var",
                file=sys.stderr,
            )
            return 1
        print(f"Applying to {db_url} ...", file=sys.stderr)
        try:
            apply_sql(db_url, sql)
        except Exception as exc:
            print(f"ERROR during apply: {exc}", file=sys.stderr)
            return 1
        print("Applied successfully.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
