"""Customer-configurable WMS schema mapping.

The default WMS schema names every table and column the way `init_db.sql`
seeds them. Real customers will have different conventions; instead of
forking the adapter, they ship a `wms_schema.yml` that maps their names
onto the canonical names the rest of the codebase uses.

The adapter builds SQL like ``SELECT bin_x AS x, bin_y AS y ...`` so row
mapping code can stay schema-agnostic.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


_DEFAULT_TABLES: dict[str, str] = {
    "locations": "locations",
    "skus": "skus",
    "inventory_positions": "inventory_positions",
    "carrier_appointments": "carrier_appointments",
    "outbound_orders": "outbound_orders",
    "order_lines": "order_lines",
    "dock_doors": "dock_doors",
}

_DEFAULT_COLUMNS: dict[str, dict[str, str]] = {
    "locations": {
        "location_id": "location_id",
        "zone": "zone",
        "aisle": "aisle",
        "bay": "bay",
        "level": "level",
        "x": "x",
        "y": "y",
        "temperature_zone": "temperature_zone",
        "max_weight_kg": "max_weight_kg",
        "max_volume_m3": "max_volume_m3",
        "is_staging": "is_staging",
        "nearest_dock_door": "nearest_dock_door",
    },
    "skus": {
        "sku_id": "sku_id",
        "description": "description",
        "weight_kg": "weight_kg",
        "volume_m3": "volume_m3",
        "hazmat_class": "hazmat_class",
        "requires_temperature_zone": "requires_temperature_zone",
        "abc_class": "abc_class",
    },
    "inventory_positions": {
        "position_id": "position_id",
        "sku_id": "sku_id",
        "location_id": "location_id",
        "quantity": "quantity",
        "lot_number": "lot_number",
        "expiry_date": "expiry_date",
    },
    "carrier_appointments": {
        "appointment_id": "appointment_id",
        "carrier": "carrier",
        "dock_door": "dock_door",
        "scheduled_arrival": "scheduled_arrival",
        "scheduled_departure": "scheduled_departure",
        "status": "status",
    },
    "outbound_orders": {
        "order_id": "order_id",
        "appointment_id": "appointment_id",
        "priority": "priority",
        "cutoff_time": "cutoff_time",
    },
    "order_lines": {
        "line_id": "line_id",
        "order_id": "order_id",
        "sku_id": "sku_id",
        "quantity": "quantity",
        "picked": "picked",
    },
    "dock_doors": {
        "dock_door": "dock_door",
        "x": "x",
        "y": "y",
        "description": "description",
    },
}


class WMSSchema(BaseModel):
    """Mapping from canonical names to a customer WMS's table and column names.

    Defaults match `scripts/init_db.sql`. Customer overrides are merged
    on top; any name not overridden falls back to the default.
    """

    tables: dict[str, str] = Field(default_factory=lambda: dict(_DEFAULT_TABLES))
    columns: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {k: dict(v) for k, v in _DEFAULT_COLUMNS.items()}
    )

    def table(self, canonical: str) -> str:
        """Return the customer's table name for a canonical table.

        Args:
            canonical: Canonical table name.

        Returns:
            The customer's table name (or the canonical name if unmapped).
        """
        return self.tables.get(canonical, canonical)

    def col(self, canonical_table: str, canonical_column: str) -> str:
        """Return the customer's column name for a (table, column) pair.

        Args:
            canonical_table: Canonical table name.
            canonical_column: Canonical column name.

        Returns:
            The customer's column name (or the canonical column if unmapped).
        """
        return self.columns.get(canonical_table, {}).get(canonical_column, canonical_column)

    def select_clause(
        self,
        canonical_table: str,
        columns: list[str],
        alias: str | None = None,
        rename: dict[str, str] | None = None,
    ) -> str:
        """Build a SELECT fragment with `customer_col AS canonical_col` aliases.

        Args:
            canonical_table: Canonical table name to look up column mappings.
            columns: Canonical column names to include.
            alias: Optional SQL table alias prefix (e.g. ``l`` for ``l.bin_x``).
            rename: Optional map of canonical column → output alias name to use
                instead of the canonical column. Useful when a query needs the
                same conceptual column under a different name to avoid SQL
                collisions (e.g. ``status`` → ``appt_status``).

        Returns:
            Comma-separated SELECT fragment.
        """
        rename = rename or {}
        prefix = f"{alias}." if alias else ""
        parts: list[str] = []
        for canonical in columns:
            customer_col = self.col(canonical_table, canonical)
            output_name = rename.get(canonical, canonical)
            if customer_col == output_name and not alias:
                parts.append(customer_col)
            else:
                parts.append(f"{prefix}{customer_col} AS {output_name}")
        return ", ".join(parts)


def load_wms_schema(path: str | Path | None) -> WMSSchema:
    """Load a WMSSchema from a YAML file, deep-merged onto defaults.

    Args:
        path: Path to wms_schema.yml. If None or missing, returns defaults.

    Returns:
        A fully populated WMSSchema instance.
    """
    schema = WMSSchema()
    if path is None:
        return schema
    p = Path(path)
    if not p.exists():
        return schema
    with open(p) as f:
        data = yaml.safe_load(f) or {}

    table_overrides = data.get("tables", {}) or {}
    for canonical, customer in table_overrides.items():
        schema.tables[canonical] = customer

    column_overrides = data.get("columns", {}) or {}
    for canonical_table, col_map in column_overrides.items():
        if canonical_table not in schema.columns:
            schema.columns[canonical_table] = {}
        for canonical_col, customer_col in (col_map or {}).items():
            schema.columns[canonical_table][canonical_col] = customer_col

    return schema
