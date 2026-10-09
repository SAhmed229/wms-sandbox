"""Tests for customer-configurable WMS schema mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingestion.schema import WMSSchema, load_wms_schema


def test_default_schema_uses_canonical_names() -> None:
    schema = WMSSchema()
    assert schema.table("locations") == "locations"
    assert schema.col("locations", "x") == "x"
    assert schema.col("inventory_positions", "sku_id") == "sku_id"


def test_table_override_falls_back_to_canonical_for_unmapped() -> None:
    schema = WMSSchema()
    schema.tables["locations"] = "warehouse_bins"
    assert schema.table("locations") == "warehouse_bins"
    assert schema.table("skus") == "skus"  # unchanged


def test_column_override_falls_back_to_canonical_for_unmapped() -> None:
    schema = WMSSchema()
    schema.columns["locations"]["x"] = "bin_x_m"
    assert schema.col("locations", "x") == "bin_x_m"
    assert schema.col("locations", "y") == "y"  # unchanged


def test_select_clause_default_emits_no_alias() -> None:
    schema = WMSSchema()
    fragment = schema.select_clause("locations", ["x", "y"])
    assert fragment == "x, y"


def test_select_clause_with_table_alias_aliases_every_column() -> None:
    schema = WMSSchema()
    fragment = schema.select_clause("locations", ["x", "y"], alias="l")
    assert fragment == "l.x AS x, l.y AS y"


def test_select_clause_renames_customer_columns_to_canonical() -> None:
    schema = WMSSchema()
    schema.columns["locations"]["x"] = "bin_x_m"
    schema.columns["locations"]["y"] = "bin_y_m"
    fragment = schema.select_clause("locations", ["x", "y"], alias="l")
    assert fragment == "l.bin_x_m AS x, l.bin_y_m AS y"


def test_select_clause_honors_rename_overrides() -> None:
    schema = WMSSchema()
    fragment = schema.select_clause(
        "carrier_appointments",
        ["status"],
        alias="a",
        rename={"status": "appt_status"},
    )
    assert fragment == "a.status AS appt_status"


def test_load_wms_schema_returns_defaults_when_path_is_none() -> None:
    schema = load_wms_schema(None)
    assert schema.table("locations") == "locations"


def test_load_wms_schema_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    schema = load_wms_schema(tmp_path / "does_not_exist.yml")
    assert schema.table("locations") == "locations"


def test_load_wms_schema_merges_overrides_onto_defaults(tmp_path: Path) -> None:
    yaml_path = tmp_path / "wms_schema.yml"
    yaml_path.write_text(
        """
        tables:
          locations: warehouse_bins
        columns:
          locations:
            x: bin_x_m
        """
    )
    schema = load_wms_schema(yaml_path)
    # Overrides applied
    assert schema.table("locations") == "warehouse_bins"
    assert schema.col("locations", "x") == "bin_x_m"
    # Defaults preserved for unmapped fields
    assert schema.table("skus") == "skus"
    assert schema.col("locations", "y") == "y"
    assert schema.col("locations", "zone") == "zone"


def test_load_wms_schema_handles_empty_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "wms_schema.yml"
    yaml_path.write_text("")
    schema = load_wms_schema(yaml_path)
    assert schema.table("locations") == "locations"


@pytest.mark.asyncio
async def test_adapter_with_renamed_schema_returns_same_data() -> None:
    """End-to-end: create a parallel set of tables with renamed columns,
    point a schema at them, and confirm the adapter returns identical rows.
    """
    import os
    from datetime import UTC, datetime, timedelta

    import redis.asyncio as aioredis
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.ingestion.adapters.generic_db import GenericDBAdapter

    db_url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://wms:wms@localhost:5433/wms"
    )
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("DB not available")
    finally:
        await engine.dispose()

    # Build a renamed view-set that aliases canonical columns to "customer" names.
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP VIEW IF EXISTS test_warehouse_bins CASCADE"))
        await conn.execute(text(
            """
            CREATE VIEW test_warehouse_bins AS
            SELECT
              location_id AS bin_id, zone, aisle, bay, level,
              x AS bin_x_m, y AS bin_y_m,
              temperature_zone, max_weight_kg, max_volume_m3,
              is_staging, nearest_dock_door
            FROM locations
            """
        ))
    await engine.dispose()

    custom_schema = WMSSchema()
    custom_schema.tables["locations"] = "test_warehouse_bins"
    custom_schema.columns["locations"]["location_id"] = "bin_id"
    custom_schema.columns["locations"]["x"] = "bin_x_m"
    custom_schema.columns["locations"]["y"] = "bin_y_m"

    r = aioredis.from_url(redis_url)
    await r.flushdb()
    # Default adapter
    default_adapter = GenericDBAdapter(database_url=db_url, redis_client=r)
    await default_adapter.connect()
    default_staging = await default_adapter.get_staging_locations()
    await default_adapter.disconnect()

    # Renamed-schema adapter pointing at the view
    await r.flushdb()
    renamed_adapter = GenericDBAdapter(
        database_url=db_url, redis_client=r, schema=custom_schema
    )
    await renamed_adapter.connect()
    renamed_staging = await renamed_adapter.get_staging_locations()
    await renamed_adapter.disconnect()

    # Cleanup view
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP VIEW IF EXISTS test_warehouse_bins CASCADE"))
    await engine.dispose()
    await r.aclose()

    assert len(default_staging) == len(renamed_staging) > 0
    default_ids = sorted(loc.location_id for loc in default_staging)
    renamed_ids = sorted(loc.location_id for loc in renamed_staging)
    assert default_ids == renamed_ids
    # Coordinates should match through the rename
    default_coords = sorted((loc.location_id, loc.x, loc.y) for loc in default_staging)
    renamed_coords = sorted((loc.location_id, loc.x, loc.y) for loc in renamed_staging)
    assert default_coords == renamed_coords
