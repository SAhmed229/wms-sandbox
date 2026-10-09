"""Tests for CSV import pipeline."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.ingestion.csv_loader import (
    CSVImportError,
    apply_sql,
    apply_sql_async,
    generate_sql,
    load_csv_dir,
)
from src.ingestion.schema import WMSSchema


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body)


def test_missing_required_columns_raises(tmp_path: Path) -> None:
    _write(tmp_path, "locations.csv", "location_id,zone\nLOC1,A\n")
    with pytest.raises(CSVImportError, match="missing required columns"):
        load_csv_dir(tmp_path)


def test_missing_optional_columns_ok(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "locations.csv",
        "location_id,zone,aisle,bay,x,y\nLOC1,A,1,1,5.0,3.0\n",
    )
    result = load_csv_dir(tmp_path)
    assert "locations" in result
    rows = result["locations"].rows
    assert rows[0]["location_id"] == "LOC1"
    assert rows[0]["x"] == 5.0
    # Unmentioned optional columns are simply absent from the row dict.
    assert "level" not in rows[0]


def test_empty_required_value_raises(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "skus.csv",
        "sku_id,description,weight_kg,volume_m3\n,thing,1.0,0.5\n",
    )
    with pytest.raises(CSVImportError, match="required column 'sku_id' is empty"):
        load_csv_dir(tmp_path)


def test_bad_type_raises_with_row_context(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "locations.csv",
        "location_id,zone,aisle,bay,x,y\nLOC1,A,not_an_int,1,5.0,3.0\n",
    )
    with pytest.raises(CSVImportError, match="row 2, column 'aisle'.*cannot parse"):
        load_csv_dir(tmp_path)


def test_boolean_accepts_common_forms(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "locations.csv",
        "location_id,zone,aisle,bay,x,y,is_staging\n"
        "L1,A,1,1,5.0,3.0,true\n"
        "L2,A,1,2,5.0,3.0,t\n"
        "L3,A,1,3,5.0,3.0,1\n"
        "L4,A,1,4,5.0,3.0,false\n",
    )
    rows = load_csv_dir(tmp_path)["locations"].rows
    assert [r["is_staging"] for r in rows] == [True, True, True, False]


def test_datetime_accepts_iso_with_z_suffix(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "carrier_appointments.csv",
        "appointment_id,carrier,dock_door,scheduled_arrival,scheduled_departure\n"
        "APPT1,ACME,1,2026-06-04T20:00:00Z,2026-06-04T21:00:00Z\n",
    )
    result = load_csv_dir(tmp_path)
    assert result["carrier_appointments"].rows[0]["scheduled_arrival"] == "2026-06-04T20:00:00Z"


def test_subset_of_csvs_is_allowed(tmp_path: Path) -> None:
    _write(tmp_path, "locations.csv", "location_id,zone,aisle,bay,x,y\nL1,A,1,1,5.0,3.0\n")
    result = load_csv_dir(tmp_path)
    assert set(result.keys()) == {"locations"}


def test_directory_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(CSVImportError, match="directory not found"):
        load_csv_dir(tmp_path / "nope")


def test_unknown_column_is_warning_not_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "locations.csv",
        "location_id,zone,aisle,bay,x,y,extra_col\nL1,A,1,1,5.0,3.0,hello\n",
    )
    result = load_csv_dir(tmp_path)
    warnings = result["locations"].warnings
    assert any("extra_col" in w for w in warnings)
    # Row still parses
    assert result["locations"].rows[0]["location_id"] == "L1"


def test_generate_sql_default_schema_emits_canonical_table_names(tmp_path: Path) -> None:
    _write(tmp_path, "locations.csv", "location_id,zone,aisle,bay,x,y\nL1,A,1,1,5.0,3.0\n")
    parsed = load_csv_dir(tmp_path)
    sql = generate_sql(parsed)
    assert "INSERT INTO locations (location_id, zone, aisle, bay, x, y) VALUES" in sql
    assert "('L1', 'A', 1, 1, 5.0, 3.0)" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql


def test_generate_sql_custom_schema_remaps_table_and_columns(tmp_path: Path) -> None:
    _write(tmp_path, "locations.csv", "location_id,zone,aisle,bay,x,y\nL1,A,1,1,5.0,3.0\n")
    parsed = load_csv_dir(tmp_path)
    schema = WMSSchema()
    schema.tables["locations"] = "warehouse_bins"
    schema.columns["locations"]["location_id"] = "bin_id"
    schema.columns["locations"]["x"] = "bin_x_m"
    sql = generate_sql(parsed, schema=schema)
    assert "INSERT INTO warehouse_bins (bin_id, zone, aisle, bay, bin_x_m, y)" in sql


def test_generate_sql_truncate_uses_fk_safe_order(tmp_path: Path) -> None:
    parsed = load_csv_dir(tmp_path)  # empty dir is allowed when generating empty SQL
    sql = generate_sql(parsed, truncate=True)
    # Children before parents: order_lines, outbound_orders, ... locations last.
    order_lines_idx = sql.index("TRUNCATE TABLE order_lines")
    outbound_idx = sql.index("TRUNCATE TABLE outbound_orders")
    locations_idx = sql.index("TRUNCATE TABLE locations")
    assert order_lines_idx < outbound_idx < locations_idx


def test_generate_sql_escapes_single_quotes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "dock_doors.csv",
        "dock_door,x,y,description\n1,10.0,0.0,Bob's dock\n",
    )
    parsed = load_csv_dir(tmp_path)
    sql = generate_sql(parsed)
    assert "'Bob''s dock'" in sql


def test_generate_sql_null_for_empty_optional(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "inventory_positions.csv",
        "position_id,sku_id,location_id,quantity,lot_number,expiry_date\n"
        "INV1,SKU1,LOC1,10,,\n",
    )
    parsed = load_csv_dir(tmp_path)
    sql = generate_sql(parsed)
    assert "('INV1', 'SKU1', 'LOC1', 10, NULL, NULL)" in sql


def test_generate_sql_no_transaction_flag(tmp_path: Path) -> None:
    _write(tmp_path, "locations.csv", "location_id,zone,aisle,bay,x,y\nL1,A,1,1,5.0,3.0\n")
    parsed = load_csv_dir(tmp_path)
    sql = generate_sql(parsed, wrap_in_transaction=False)
    assert "BEGIN;" not in sql
    assert "COMMIT;" not in sql


def test_apply_sql_uses_injected_executor() -> None:
    calls: list[tuple[str, str]] = []
    apply_sql("dummy://url", "SELECT 1;", executor=lambda u, s: calls.append((u, s)))
    assert calls == [("dummy://url", "SELECT 1;")]


@pytest.mark.asyncio
async def test_e2e_sample_csvs_roundtrip_through_renamed_schema(tmp_path: Path) -> None:
    """Import the sample CSVs into a parallel renamed table and verify the
    adapter reads it back identically.
    """
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

    # Create a renamed parallel table (just locations is enough to prove the path)
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS csv_test_bins CASCADE"))
        await conn.execute(text(
            """
            CREATE TABLE csv_test_bins (
              bin_id VARCHAR(50) PRIMARY KEY,
              zone VARCHAR(20) NOT NULL,
              aisle INT NOT NULL,
              bay INT NOT NULL,
              level INT NOT NULL DEFAULT 0,
              bin_x_m NUMERIC(8,2) NOT NULL,
              bin_y_m NUMERIC(8,2) NOT NULL,
              temperature_zone VARCHAR(20) NOT NULL DEFAULT 'AMBIENT',
              max_weight_kg NUMERIC(8,2) NOT NULL DEFAULT 2000.0,
              max_volume_m3 NUMERIC(8,2) NOT NULL DEFAULT 10.0,
              is_staging BOOLEAN NOT NULL DEFAULT FALSE,
              nearest_dock_door INT
            )
            """
        ))
    await engine.dispose()

    # Write a tiny CSV
    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    _write(
        csv_dir,
        "locations.csv",
        "location_id,zone,aisle,bay,level,x,y,temperature_zone,"
        "max_weight_kg,max_volume_m3,is_staging,nearest_dock_door\n"
        "STG1,STAGING,1,1,0,5.0,3.0,AMBIENT,2500.0,12.0,true,1\n"
        "STG2,STAGING,1,2,0,15.0,3.0,AMBIENT,2500.0,12.0,true,1\n",
    )

    schema = WMSSchema()
    schema.tables["locations"] = "csv_test_bins"
    schema.columns["locations"]["location_id"] = "bin_id"
    schema.columns["locations"]["x"] = "bin_x_m"
    schema.columns["locations"]["y"] = "bin_y_m"

    parsed = load_csv_dir(csv_dir)
    sql = generate_sql(parsed, schema=schema)
    await apply_sql_async(db_url, sql)

    r = aioredis.from_url(redis_url)
    await r.flushdb()
    adapter = GenericDBAdapter(database_url=db_url, redis_client=r, schema=schema)
    await adapter.connect()
    staging = await adapter.get_staging_locations()
    await adapter.disconnect()
    await r.aclose()

    # Cleanup
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS csv_test_bins CASCADE"))
    await engine.dispose()

    ids = sorted(loc.location_id for loc in staging)
    assert ids == ["STG1", "STG2"]
    coords = sorted((loc.location_id, loc.x, loc.y) for loc in staging)
    assert coords == [("STG1", 5.0, 3.0), ("STG2", 15.0, 3.0)]
