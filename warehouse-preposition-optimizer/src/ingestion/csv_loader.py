"""CSV import pipeline for seeding/refreshing a customer's WMS schema.

Customers without CAD (DXF) tooling can drop one CSV per canonical table
into a directory and produce schema-mapped INSERT SQL — or apply it
directly to the configured database.

Canonical CSV headers always match the names in `scripts/init_db.sql`.
The customer's actual database table/column names are looked up through
:class:`WMSSchema`, so the same CSV format works whether the customer
ran `init_db.sql` verbatim or renamed everything.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.ingestion.schema import WMSSchema


class CSVImportError(ValueError):
    """Raised when a CSV file is missing required columns or contains bad data."""


@dataclass(frozen=True)
class _ColumnSpec:
    """Type + required-ness metadata for one canonical column."""

    name: str
    sql_type: str  # "str" | "int" | "float" | "bool" | "datetime"
    required: bool = True


@dataclass(frozen=True)
class _TableSpec:
    """Per-table CSV schema: filename, canonical columns, FK dependencies."""

    canonical_name: str
    csv_filename: str
    columns: tuple[_ColumnSpec, ...]
    depends_on: tuple[str, ...] = ()


# Insert order respects FK dependencies (parents first).
TABLE_SPECS: tuple[_TableSpec, ...] = (
    _TableSpec(
        canonical_name="locations",
        csv_filename="locations.csv",
        columns=(
            _ColumnSpec("location_id", "str"),
            _ColumnSpec("zone", "str"),
            _ColumnSpec("aisle", "int"),
            _ColumnSpec("bay", "int"),
            _ColumnSpec("level", "int", required=False),
            _ColumnSpec("x", "float"),
            _ColumnSpec("y", "float"),
            _ColumnSpec("temperature_zone", "str", required=False),
            _ColumnSpec("max_weight_kg", "float", required=False),
            _ColumnSpec("max_volume_m3", "float", required=False),
            _ColumnSpec("is_staging", "bool", required=False),
            _ColumnSpec("nearest_dock_door", "int", required=False),
        ),
    ),
    _TableSpec(
        canonical_name="dock_doors",
        csv_filename="dock_doors.csv",
        columns=(
            _ColumnSpec("dock_door", "int"),
            _ColumnSpec("x", "float"),
            _ColumnSpec("y", "float"),
            _ColumnSpec("description", "str", required=False),
        ),
    ),
    _TableSpec(
        canonical_name="skus",
        csv_filename="skus.csv",
        columns=(
            _ColumnSpec("sku_id", "str"),
            _ColumnSpec("description", "str"),
            _ColumnSpec("weight_kg", "float"),
            _ColumnSpec("volume_m3", "float"),
            _ColumnSpec("hazmat_class", "str", required=False),
            _ColumnSpec("requires_temperature_zone", "str", required=False),
            _ColumnSpec("abc_class", "str", required=False),
        ),
    ),
    _TableSpec(
        canonical_name="inventory_positions",
        csv_filename="inventory_positions.csv",
        columns=(
            _ColumnSpec("position_id", "str"),
            _ColumnSpec("sku_id", "str"),
            _ColumnSpec("location_id", "str"),
            _ColumnSpec("quantity", "int"),
            _ColumnSpec("lot_number", "str", required=False),
            _ColumnSpec("expiry_date", "datetime", required=False),
        ),
        depends_on=("skus", "locations"),
    ),
    _TableSpec(
        canonical_name="carrier_appointments",
        csv_filename="carrier_appointments.csv",
        columns=(
            _ColumnSpec("appointment_id", "str"),
            _ColumnSpec("carrier", "str"),
            _ColumnSpec("dock_door", "int"),
            _ColumnSpec("scheduled_arrival", "datetime"),
            _ColumnSpec("scheduled_departure", "datetime"),
            _ColumnSpec("status", "str", required=False),
        ),
    ),
    _TableSpec(
        canonical_name="outbound_orders",
        csv_filename="outbound_orders.csv",
        columns=(
            _ColumnSpec("order_id", "str"),
            _ColumnSpec("appointment_id", "str"),
            _ColumnSpec("priority", "int"),
            _ColumnSpec("cutoff_time", "datetime"),
        ),
        depends_on=("carrier_appointments",),
    ),
    _TableSpec(
        canonical_name="order_lines",
        csv_filename="order_lines.csv",
        columns=(
            _ColumnSpec("line_id", "str"),
            _ColumnSpec("order_id", "str"),
            _ColumnSpec("sku_id", "str"),
            _ColumnSpec("quantity", "int"),
            _ColumnSpec("picked", "bool", required=False),
        ),
        depends_on=("outbound_orders", "skus"),
    ),
)

# FK-safe truncation order: children before parents.
_TRUNCATE_ORDER: tuple[str, ...] = (
    "order_lines",
    "outbound_orders",
    "carrier_appointments",
    "inventory_positions",
    "skus",
    "dock_doors",
    "locations",
)


@dataclass
class TableLoadResult:
    """Parsed rows for one table.

    Args:
        canonical_name: Canonical table name.
        rows: List of {canonical_column: parsed_value} dicts.
        present_columns: Canonical column names actually present in the CSV.
        warnings: Non-fatal issues encountered during parsing.
    """

    canonical_name: str
    rows: list[dict[str, Any]]
    present_columns: list[str]
    warnings: list[str] = field(default_factory=list)


def _coerce(value: str, sql_type: str, column: str, row_num: int) -> Any:
    """Convert a CSV cell into a typed Python value, or raise on bad input."""
    if value == "" or value is None:
        return None
    try:
        if sql_type == "str":
            return value
        if sql_type == "int":
            return int(value)
        if sql_type == "float":
            return float(value)
        if sql_type == "bool":
            v = value.strip().lower()
            if v in {"true", "t", "1", "yes", "y"}:
                return True
            if v in {"false", "f", "0", "no", "n"}:
                return False
            raise ValueError(f"unrecognized boolean: {value!r}")
        if sql_type == "datetime":
            # Accept ISO 8601 (optionally with Z), pass through to Postgres as a quoted
            # string. Validate by attempting to parse here.
            v = value.strip()
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                # Some sources use space instead of T; try once more.
                datetime.fromisoformat(v.replace(" ", "T").replace("Z", "+00:00"))
            return v
        raise ValueError(f"unknown sql_type: {sql_type}")
    except (ValueError, TypeError) as exc:
        raise CSVImportError(
            f"row {row_num}, column '{column}': cannot parse {value!r} as {sql_type} ({exc})"
        ) from None


def load_csv_dir(directory: str | Path) -> dict[str, TableLoadResult]:
    """Parse all known canonical CSVs found in ``directory``.

    Files not present are silently skipped (a customer may load a subset).
    Required columns missing from a present file raises :class:`CSVImportError`.

    Args:
        directory: Directory containing canonical CSV files.

    Returns:
        Map of canonical_name → TableLoadResult for every CSV that was found.
    """
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        raise CSVImportError(f"directory not found: {dir_path}")

    results: dict[str, TableLoadResult] = {}
    for spec in TABLE_SPECS:
        csv_path = dir_path / spec.csv_filename
        if not csv_path.exists():
            continue
        results[spec.canonical_name] = _load_one(csv_path, spec)
    return results


def _load_one(csv_path: Path, spec: _TableSpec) -> TableLoadResult:
    """Parse one CSV against its table spec."""
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        spec_cols = {c.name: c for c in spec.columns}
        unknown = [h for h in headers if h not in spec_cols]
        present = [c.name for c in spec.columns if c.name in headers]

        missing_required = [
            c.name for c in spec.columns if c.required and c.name not in headers
        ]
        if missing_required:
            raise CSVImportError(
                f"{csv_path.name} missing required columns: {missing_required}"
            )

        warnings: list[str] = []
        if unknown:
            warnings.append(
                f"{csv_path.name}: ignoring unknown columns {unknown}"
            )

        rows: list[dict[str, Any]] = []
        for i, raw in enumerate(reader, start=2):  # row 2 = first data row
            parsed: dict[str, Any] = {}
            for col in present:
                spec_col = spec_cols[col]
                parsed[col] = _coerce(raw.get(col, ""), spec_col.sql_type, col, i)
                if spec_col.required and parsed[col] is None:
                    raise CSVImportError(
                        f"{csv_path.name} row {i}: required column '{col}' is empty"
                    )
            rows.append(parsed)

    return TableLoadResult(
        canonical_name=spec.canonical_name,
        rows=rows,
        present_columns=present,
        warnings=warnings,
    )


def _sql_literal(value: Any, sql_type: str) -> str:
    """Render a Python value as a PostgreSQL literal."""
    if value is None:
        return "NULL"
    if sql_type == "str":
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    if sql_type == "int":
        return str(int(value))
    if sql_type == "float":
        return repr(float(value))
    if sql_type == "bool":
        return "TRUE" if value else "FALSE"
    if sql_type == "datetime":
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    raise ValueError(f"unknown sql_type: {sql_type}")


def generate_sql(
    parsed: dict[str, TableLoadResult],
    schema: WMSSchema | None = None,
    truncate: bool = False,
    wrap_in_transaction: bool = True,
) -> str:
    """Render parsed CSV rows as INSERT SQL targeted at the customer's schema.

    Args:
        parsed: Output of :func:`load_csv_dir`.
        schema: WMSSchema for translating canonical → customer names. Defaults
            to canonical (un-renamed) names matching init_db.sql.
        truncate: If True, prepend ``TRUNCATE`` statements for the seven
            tables in FK-safe child→parent order before inserting.
        wrap_in_transaction: If True, wrap the output in ``BEGIN; ... COMMIT;``
            so a failed INSERT mid-import rolls back cleanly.

    Returns:
        Complete SQL script as a string.
    """
    sch = schema or WMSSchema()
    type_lookup: dict[str, dict[str, str]] = {
        s.canonical_name: {c.name: c.sql_type for c in s.columns} for s in TABLE_SPECS
    }

    lines: list[str] = [
        "-- ── Generated by scripts/import_csv.py ──────────────────────────────",
    ]
    for canonical, result in parsed.items():
        lines.append(f"-- {canonical}: {len(result.rows)} rows")
    lines.append("")

    if wrap_in_transaction:
        lines.append("BEGIN;")
        lines.append("")

    if truncate:
        lines.append("-- Truncate existing data (FK-safe order: children first)")
        for canonical in _TRUNCATE_ORDER:
            customer = sch.table(canonical)
            lines.append(f"TRUNCATE TABLE {customer} CASCADE;")
        lines.append("")

    # Insert in dependency-respecting order (TABLE_SPECS is already ordered).
    for spec in TABLE_SPECS:
        result = parsed.get(spec.canonical_name)
        if not result or not result.rows:
            continue
        customer_table = sch.table(spec.canonical_name)
        customer_cols = [sch.col(spec.canonical_name, c) for c in result.present_columns]
        col_list = ", ".join(customer_cols)
        lines.append(f"-- {spec.canonical_name} ({len(result.rows)} rows)")
        lines.append(f"INSERT INTO {customer_table} ({col_list}) VALUES")
        value_rows: list[str] = []
        for row in result.rows:
            literals = [
                _sql_literal(row[c], type_lookup[spec.canonical_name][c])
                for c in result.present_columns
            ]
            value_rows.append(f"    ({', '.join(literals)})")
        lines.append(",\n".join(value_rows) + ";")
        lines.append("")

    if wrap_in_transaction:
        lines.append("COMMIT;")

    return "\n".join(lines).rstrip() + "\n"


async def apply_sql_async(database_url: str, sql: str) -> None:
    """Async variant of :func:`apply_sql` for use from within an event loop."""
    import asyncpg

    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "+asyncpg", ""
    )
    conn = await asyncpg.connect(pg_url)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def apply_sql(
    database_url: str,
    sql: str,
    executor: Callable[[str, str], None] | None = None,
) -> None:
    """Execute the generated SQL against a Postgres database.

    Uses asyncpg directly (no extra driver dependency). For callers already
    inside an event loop, use :func:`apply_sql_async` instead.

    Args:
        database_url: Postgres URL (with or without ``+asyncpg``).
        sql: SQL script to execute.
        executor: Optional injection point for tests; bypasses the asyncpg path.
    """
    if executor is not None:
        executor(database_url, sql)
        return

    import asyncio

    asyncio.run(apply_sql_async(database_url, sql))
