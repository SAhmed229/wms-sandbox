"""Tests for src/ingestion/floor_plan_parser.py.

All tests use ezdxf's in-memory drawing API so no file I/O is required.
A temporary .dxf file is written only where the full parse() path needs it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf", reason="ezdxf not installed")

from src.ingestion.floor_plan_parser import (
    FloorPlanParser,
    FloorPlanResult,
    LayerConfig,
    ParsedDockDoor,
    ParsedLocation,
    _deduplicate_locs,
    _entity_centroid,
    _extract_location_id,
    _group_by_tolerance,
    _nearest_dock,
    _RawLoc,
    generate_dock_coords_python,
    generate_sql,
)
from src.models.inventory import TemperatureZone


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_dxf(
    rack_positions: list[tuple[float, float, str]] | None = None,
    staging_positions: list[tuple[float, float, str]] | None = None,
    dock_positions: list[tuple[float, float]] | None = None,
    frozen_positions: list[tuple[float, float, str]] | None = None,
    chilled_positions: list[tuple[float, float, str]] | None = None,
    insunits: int = 6,  # 6 = metres
) -> Path:
    """Build an in-memory DXF drawing and save to a temp file.

    Each position tuple is (x, y, location_id).  The location ID is stored
    as a block attribute with tag ``LOCID``.
    """
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()

    # Create a reusable block with a LOCID attribute
    blk = doc.blocks.new("RACK")
    blk.add_attdef("LOCID", (0, 0), dxfattribs={"layer": "0"})

    def _add_block(layer: str, x: float, y: float, loc_id: str) -> None:
        ref = msp.add_blockref("RACK", (x, y), dxfattribs={"layer": layer})
        ref.add_auto_attribs({"LOCID": loc_id})

    def _add_point(layer: str, x: float, y: float) -> None:
        msp.add_point((x, y), dxfattribs={"layer": layer})

    for x, y, lid in (rack_positions or []):
        _add_block("RACK_POSITIONS", x, y, lid)

    for x, y, lid in (staging_positions or []):
        _add_block("STAGING_LANES", x, y, lid)

    for x, y in (dock_positions or []):
        _add_point("DOCK_DOORS", x, y)

    for x, y, lid in (frozen_positions or []):
        _add_block("FREEZER_ZONE", x, y, lid)

    for x, y, lid in (chilled_positions or []):
        _add_block("CHILLER_ZONE", x, y, lid)

    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    tmp.close()
    doc.saveas(tmp.name)
    return Path(tmp.name)


_DEFAULT_CONFIG = LayerConfig(
    locations_layers=["RACK_POSITIONS"],
    staging_layers=["STAGING_LANES"],
    dock_layers=["DOCK_DOORS"],
    frozen_layers=["FREEZER_ZONE"],
    chilled_layers=["CHILLER_ZONE"],
    location_id_attrib_tag="LOCID",
)


# ─────────────────────────────────────────────────────────────────────────────
# Unit helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_group_by_tolerance_clusters_close_values() -> None:
    values = [0.0, 0.1, 0.4, 5.0, 5.3, 10.0]
    groups = _group_by_tolerance(values, tolerance=0.5)
    assert groups[0.0] == groups[0.1] == groups[0.4]
    assert groups[5.0] == groups[5.3]
    assert groups[10.0] != groups[0.0]


def test_group_by_tolerance_separate_distinct_values() -> None:
    values = [0.0, 2.0, 4.0]
    groups = _group_by_tolerance(values, tolerance=0.5)
    assert groups[0.0] != groups[2.0] != groups[4.0]


def test_nearest_dock_returns_correct_door() -> None:
    docks = [ParsedDockDoor(1, 10.0, 0.0), ParsedDockDoor(2, 40.0, 0.0)]
    door_id, dist = _nearest_dock(12.0, 15.0, docks)
    assert door_id == 1
    assert dist < 20.0


def test_nearest_dock_empty_list() -> None:
    door_id, dist = _nearest_dock(10.0, 10.0, [])
    assert door_id is None
    assert dist == float("inf")


def test_deduplicate_locs_removes_nearby_duplicate() -> None:
    raw = [
        _RawLoc(0.0, 0.0, "L1", False, TemperatureZone.AMBIENT, "RACK"),
        _RawLoc(0.05, 0.05, "L2", False, TemperatureZone.AMBIENT, "RACK"),  # within 0.1 m
        _RawLoc(5.0, 5.0, "L3", False, TemperatureZone.AMBIENT, "RACK"),
    ]
    deduped = _deduplicate_locs(raw, tolerance=0.1)
    assert len(deduped) == 2


def test_deduplicate_locs_keeps_distinct_positions() -> None:
    raw = [
        _RawLoc(0.0, 0.0, "L1", False, TemperatureZone.AMBIENT, "RACK"),
        _RawLoc(1.0, 0.0, "L2", False, TemperatureZone.AMBIENT, "RACK"),
        _RawLoc(0.0, 1.0, "L3", False, TemperatureZone.AMBIENT, "RACK"),
    ]
    deduped = _deduplicate_locs(raw, tolerance=0.1)
    assert len(deduped) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Entity helpers (using ezdxf in-memory API)
# ─────────────────────────────────────────────────────────────────────────────


def test_entity_centroid_insert() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    ref = msp.add_blockref("TEST", (15.0, 25.0))
    cx, cy = _entity_centroid(ref)
    assert cx == pytest.approx(15.0)
    assert cy == pytest.approx(25.0)


def test_entity_centroid_point() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    pt = msp.add_point((7.5, 3.2))
    cx, cy = _entity_centroid(pt)
    assert cx == pytest.approx(7.5)
    assert cy == pytest.approx(3.2)


def test_entity_centroid_circle() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    circ = msp.add_circle((4.0, 8.0), radius=1.0)
    cx, cy = _entity_centroid(circ)
    assert cx == pytest.approx(4.0)
    assert cy == pytest.approx(8.0)


def test_entity_centroid_lwpolyline_rectangle() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    # 4-point rectangle with centroid at (5.0, 5.0)
    poly = msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    cx, cy = _entity_centroid(poly)
    assert cx == pytest.approx(5.0)
    assert cy == pytest.approx(5.0)


def test_entity_centroid_text() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    txt = msp.add_text("A01-001", dxfattribs={"insert": (3.0, 7.0)})
    cx, cy = _entity_centroid(txt)
    assert cx == pytest.approx(3.0)
    assert cy == pytest.approx(7.0)


def test_entity_centroid_unknown_returns_none() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    line = msp.add_line((0, 0), (10, 10))
    # LINE centroid is midpoint
    cx, cy = _entity_centroid(line)
    assert cx == pytest.approx(5.0)
    assert cy == pytest.approx(5.0)


def test_extract_location_id_from_attrib() -> None:
    doc = ezdxf.new()
    blk = doc.blocks.new("TESTBLK")
    blk.add_attdef("LOCID", (0, 0))
    msp = doc.modelspace()
    ref = msp.add_blockref("TESTBLK", (0, 0))
    ref.add_auto_attribs({"LOCID": "AA-001-0"})
    loc_id = _extract_location_id(ref, "LOCID")
    assert loc_id == "AA-001-0"


def test_extract_location_id_case_insensitive_tag() -> None:
    doc = ezdxf.new()
    blk = doc.blocks.new("TESTBLK2")
    blk.add_attdef("LOCID", (0, 0))
    msp = doc.modelspace()
    ref = msp.add_blockref("TESTBLK2", (0, 0))
    ref.add_auto_attribs({"LOCID": "BB-002-1"})
    # Search with different case
    loc_id = _extract_location_id(ref, "locid")
    assert loc_id == "BB-002-1"


def test_extract_location_id_from_text_entity() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    txt = msp.add_text("STAGE-001", dxfattribs={"insert": (0, 0)})
    loc_id = _extract_location_id(txt, "LOCID")
    assert loc_id == "STAGE-001"


def test_extract_location_id_no_attrib_returns_none() -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    ref = msp.add_blockref("NOATTRIB", (0, 0))
    loc_id = _extract_location_id(ref, "LOCID")
    assert loc_id is None


# ─────────────────────────────────────────────────────────────────────────────
# FloorPlanParser — list_layers
# ─────────────────────────────────────────────────────────────────────────────


def test_list_layers_returns_layer_names(tmp_path: Path) -> None:
    doc = ezdxf.new()
    doc.layers.add("RACK_POSITIONS")
    doc.layers.add("DOCK_DOORS")
    dxf_path = tmp_path / "test.dxf"
    doc.saveas(str(dxf_path))

    parser = FloorPlanParser()
    layers = parser.list_layers(dxf_path)

    assert "RACK_POSITIONS" in layers
    assert "DOCK_DOORS" in layers
    assert layers == sorted(layers)


def test_list_layers_raises_for_dwg(tmp_path: Path) -> None:
    dwg_path = tmp_path / "layout.dwg"
    dwg_path.write_bytes(b"AC1015")  # fake DWG header

    parser = FloorPlanParser()
    with pytest.raises(ValueError, match="DWG"):
        parser.list_layers(dwg_path)


def test_list_layers_raises_for_missing_file(tmp_path: Path) -> None:
    parser = FloorPlanParser()
    with pytest.raises(FileNotFoundError):
        parser.list_layers(tmp_path / "nonexistent.dxf")


# ─────────────────────────────────────────────────────────────────────────────
# FloorPlanParser — parse()
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_extracts_locations_and_dock_doors() -> None:
    dxf_path = _make_dxf(
        rack_positions=[(10.0, 20.0, "LOC-001"), (25.0, 20.0, "LOC-002")],
        dock_positions=[(10.0, 0.0), (40.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    assert len(result.locations) == 2
    assert len(result.dock_doors) == 2
    assert result.dock_doors[0].door_id == 1
    assert result.dock_doors[0].x == pytest.approx(10.0)
    assert result.dock_doors[1].door_id == 2
    assert result.dock_doors[1].x == pytest.approx(40.0)


def test_parse_location_ids_extracted_from_attribs() -> None:
    dxf_path = _make_dxf(
        rack_positions=[(10.0, 20.0, "AA-01-0")],
        dock_positions=[(10.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    assert result.locations[0].location_id == "AA-01-0"


def test_parse_auto_generates_location_ids_when_no_attrib() -> None:
    """POINT entities have no attribute → IDs should be auto-generated."""
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 6
    msp = doc.modelspace()
    msp.add_point((10.0, 20.0), dxfattribs={"layer": "RACK_POSITIONS"})
    msp.add_point((25.0, 20.0), dxfattribs={"layer": "RACK_POSITIONS"})
    msp.add_point((10.0, 0.0), dxfattribs={"layer": "DOCK_DOORS"})

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    tmp.close()
    doc.saveas(tmp.name)

    parser = FloorPlanParser()
    result = parser.parse(Path(tmp.name), _DEFAULT_CONFIG)

    assert len(result.locations) == 2
    ids = {loc.location_id for loc in result.locations}
    assert all(id_.startswith("A") for id_ in ids)


def test_parse_staging_locations_flagged() -> None:
    dxf_path = _make_dxf(
        staging_positions=[(10.0, 3.0, "STAGE-001"), (40.0, 3.0, "STAGE-002")],
        dock_positions=[(10.0, 0.0), (40.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    assert all(loc.is_staging for loc in result.locations)


def test_parse_frozen_temperature_zone() -> None:
    dxf_path = _make_dxf(
        frozen_positions=[(10.0, 70.0, "FRZ-001")],
        dock_positions=[(10.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    frozen_locs = [l for l in result.locations if l.temperature_zone == TemperatureZone.FROZEN]
    assert len(frozen_locs) == 1
    assert frozen_locs[0].location_id == "FRZ-001"


def test_parse_chilled_temperature_zone() -> None:
    dxf_path = _make_dxf(
        chilled_positions=[(10.0, 65.0, "CHL-001")],
        dock_positions=[(10.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    chilled = [l for l in result.locations if l.temperature_zone == TemperatureZone.CHILLED]
    assert len(chilled) == 1


def test_parse_nearest_dock_door_assigned() -> None:
    dxf_path = _make_dxf(
        rack_positions=[(8.0, 20.0, "NEAR-D1"), (42.0, 20.0, "NEAR-D2")],
        dock_positions=[(10.0, 0.0), (40.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    by_id = {loc.location_id: loc for loc in result.locations}
    assert by_id["NEAR-D1"].nearest_dock_door == 1
    assert by_id["NEAR-D2"].nearest_dock_door == 2


def test_parse_aisle_and_bay_assigned() -> None:
    dxf_path = _make_dxf(
        rack_positions=[
            (10.0, 20.0, "A1-B1"), (25.0, 20.0, "A1-B2"),  # same aisle (y≈20)
            (10.0, 35.0, "A2-B1"),                           # different aisle (y≈35)
        ],
        dock_positions=[(10.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    by_id = {loc.location_id: loc for loc in result.locations}
    assert by_id["A1-B1"].aisle == by_id["A1-B2"].aisle
    assert by_id["A1-B1"].aisle != by_id["A2-B1"].aisle
    assert by_id["A1-B1"].bay == 1
    assert by_id["A1-B2"].bay == 2


def test_parse_dock_doors_sorted_left_to_right() -> None:
    dxf_path = _make_dxf(
        rack_positions=[(10.0, 20.0, "L1")],
        dock_positions=[(100.0, 0.0), (10.0, 0.0), (55.0, 0.0)],
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    xs = [d.x for d in result.dock_doors]
    assert xs == sorted(xs)
    assert result.dock_doors[0].door_id == 1


def test_parse_unit_conversion_feet_to_metres() -> None:
    """Locations specified in feet should be converted to metres."""
    dxf_path = _make_dxf(
        rack_positions=[(32.808, 65.617, "LOC-FT")],  # ≈ 10 m, 20 m in feet
        dock_positions=[(32.808, 0.0)],
        insunits=2,  # feet
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    loc = result.locations[0]
    assert loc.x == pytest.approx(10.0, abs=0.01)
    assert loc.y == pytest.approx(20.0, abs=0.01)
    assert result.source_units == "feet"


def test_parse_unit_override_beats_insunits() -> None:
    """--unit override should take precedence over $INSUNITS header."""
    # File claims metres (insunits=6) but we override to mm
    dxf_path = _make_dxf(
        rack_positions=[(10000.0, 20000.0, "LOC-MM")],
        dock_positions=[(10000.0, 0.0)],
        insunits=6,  # metres (but the values are really mm)
    )
    config = LayerConfig(
        locations_layers=["RACK_POSITIONS"],
        dock_layers=["DOCK_DOORS"],
        unit_override="mm",
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, config)

    loc = result.locations[0]
    assert loc.x == pytest.approx(10.0, abs=0.01)
    assert loc.y == pytest.approx(20.0, abs=0.01)


def test_parse_missing_dock_layer_generates_warning() -> None:
    dxf_path = _make_dxf(
        rack_positions=[(10.0, 20.0, "LOC-1")],
        # No dock positions
    )
    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    assert any("dock" in w.lower() or "door" in w.lower() for w in result.warnings)


def test_parse_no_entities_generates_warning() -> None:
    doc = ezdxf.new()
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    tmp.close()
    doc.saveas(tmp.name)

    parser = FloorPlanParser()
    result = parser.parse(Path(tmp.name), _DEFAULT_CONFIG)

    assert len(result.locations) == 0
    assert any("location" in w.lower() for w in result.warnings)


def test_parse_dwg_raises_value_error(tmp_path: Path) -> None:
    dwg_path = tmp_path / "layout.dwg"
    dwg_path.write_bytes(b"AC1015fake")

    parser = FloorPlanParser()
    with pytest.raises(ValueError, match="DWG"):
        parser.parse(dwg_path, _DEFAULT_CONFIG)


# ─────────────────────────────────────────────────────────────────────────────
# SQL and Python dict generation
# ─────────────────────────────────────────────────────────────────────────────


def _make_result(
    n_locs: int = 3,
    n_doors: int = 2,
    has_staging: bool = False,
) -> FloorPlanResult:
    doors = [ParsedDockDoor(i + 1, float(i * 30 + 10), 0.0) for i in range(n_doors)]
    locs = [
        ParsedLocation(
            location_id=f"LOC-{i:03d}",
            x=float(10 + i * 15),
            y=20.0,
            temperature_zone=TemperatureZone.AMBIENT,
            is_staging=has_staging and i == 0,
            nearest_dock_door=1,
            source_layer="RACK_POSITIONS",
            aisle=1,
            bay=i + 1,
        )
        for i in range(n_locs)
    ]
    return FloorPlanResult(
        locations=locs,
        dock_doors=doors,
        unit_scale=1.0,
        source_units="metres",
        warnings=[],
    )


def test_generate_sql_contains_locations_insert() -> None:
    result = _make_result()
    sql = generate_sql(result)
    assert "INSERT INTO locations" in sql
    assert "LOC-000" in sql
    assert "LOC-001" in sql


def test_generate_sql_contains_dock_doors_insert() -> None:
    result = _make_result()
    sql = generate_sql(result)
    assert "INSERT INTO dock_doors" in sql
    assert "(1," in sql


def test_generate_sql_truncate_flag() -> None:
    result = _make_result()
    sql = generate_sql(result, clear_existing=True)
    assert "TRUNCATE" in sql.upper()


def test_generate_sql_staging_zone_label() -> None:
    result = _make_result(has_staging=True)
    sql = generate_sql(result)
    assert "'STAGING'" in sql


def test_generate_dock_coords_python_format() -> None:
    result = _make_result(n_doors=2)
    py = generate_dock_coords_python(result)
    assert "dock_door_coords" in py
    assert "1:" in py
    assert "2:" in py


def test_generate_dock_coords_python_empty() -> None:
    result = _make_result(n_doors=0)
    py = generate_dock_coords_python(result)
    assert "dock_door_coords = {}" in py


def test_generate_sql_no_locations() -> None:
    result = FloorPlanResult(
        locations=[],
        dock_doors=[ParsedDockDoor(1, 10.0, 0.0)],
        unit_scale=1.0,
        source_units="metres",
        warnings=[],
    )
    sql = generate_sql(result)
    assert "INSERT INTO dock_doors" in sql
    assert "INSERT INTO locations" not in sql


# ─────────────────────────────────────────────────────────────────────────────
# Full round-trip test
# ─────────────────────────────────────────────────────────────────────────────


def test_full_round_trip_produces_valid_sql() -> None:
    """Simulate parsing a 120×80 m warehouse DXF and generating SQL."""
    dxf_path = _make_dxf(
        rack_positions=[
            (10.0, 20.0, "A01-B01"), (25.0, 20.0, "A01-B02"), (40.0, 20.0, "A01-B03"),
            (10.0, 35.0, "A02-B01"), (25.0, 35.0, "A02-B02"),
        ],
        staging_positions=[
            (10.0, 3.0, "STAGE-001"), (40.0, 3.0, "STAGE-002"),
        ],
        dock_positions=[(10.0, 0.0), (40.0, 0.0), (70.0, 0.0)],
        frozen_positions=[(15.0, 70.0, "FRZ-001")],
    )

    parser = FloorPlanParser()
    result = parser.parse(dxf_path, _DEFAULT_CONFIG)

    assert len(result.dock_doors) == 3
    assert len(result.locations) >= 7  # 5 rack + 2 staging + 1 frozen

    staging = [l for l in result.locations if l.is_staging]
    frozen = [l for l in result.locations if l.temperature_zone == TemperatureZone.FROZEN]
    assert len(staging) == 2
    assert len(frozen) == 1

    sql = generate_sql(result)
    assert sql.count("INSERT INTO") == 2
    assert "STAGE" in sql
    assert "FROZEN" in sql
