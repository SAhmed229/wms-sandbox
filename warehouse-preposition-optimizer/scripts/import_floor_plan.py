#!/usr/bin/env python
"""Import warehouse floor plan from a DXF file.

Reads an AutoCAD DXF file, extracts location positions and dock doors from
named layers, and emits SQL INSERT statements compatible with init_db.sql.

Quick start
-----------
1. List layer names in your file::

    uv run python scripts/import_floor_plan.py layout.dxf --list-layers

2. Run the full import, mapping layers to feature types::

    uv run python scripts/import_floor_plan.py layout.dxf \\
        --locations-layer RACK_POSITIONS \\
        --staging-layer   STAGING_LANES \\
        --dock-layer      DOCK_DOORS \\
        --frozen-layer    FREEZER_ZONE \\
        --chilled-layer   CHILLER_ZONE \\
        --out scripts/init_db_imported.sql

3. Review the generated SQL, then apply it::

    docker compose exec postgres psql -U wms -d wms_prepos -f /app/init_db_imported.sql

DWG → DXF conversion
---------------------
ezdxf cannot read DWG binary files directly.  Convert first with one of:

* AutoCAD / AutoCAD LT : SAVEAS → AutoCAD 2018 DXF
* ODA File Converter   : https://www.opendesign.com/guestfiles/oda_file_converter
* LibreCAD             : File → Export → DXF
* DraftSight (free)    : File → Save As → DXF R2018

Usage examples
--------------
List all layers::

    uv run python scripts/import_floor_plan.py warehouse.dxf --list-layers

Import with multiple location layers and auto-detect units::

    uv run python scripts/import_floor_plan.py warehouse.dxf \\
        --locations-layer RACKS_A --locations-layer RACKS_B \\
        --staging-layer STAGING \\
        --dock-layer DOCK_DOORS \\
        --unit feet \\
        --out scripts/floor_plan.sql

Dry-run (print SQL without writing a file)::

    uv run python scripts/import_floor_plan.py warehouse.dxf \\
        --locations-layer RACKS --dock-layer DOORS

Append to existing init_db.sql (clears old location/dock rows first)::

    uv run python scripts/import_floor_plan.py warehouse.dxf \\
        --locations-layer RACKS --dock-layer DOORS \\
        --out scripts/init_db.sql --truncate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a script from the project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ingestion.floor_plan_parser import (
    FloorPlanParser,
    LayerConfig,
    generate_dock_coords_python,
    generate_sql,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Import warehouse floor plan from a DXF file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument("dxf_file", metavar="DXF_FILE", help="Path to .dxf file.")

    p.add_argument(
        "--list-layers",
        action="store_true",
        help="Print all layer names found in the file and exit.",
    )

    p.add_argument(
        "--locations-layer",
        metavar="LAYER",
        action="append",
        dest="locations_layers",
        default=[],
        help="DXF layer containing bulk storage rack positions.  "
             "Can be specified multiple times.",
    )
    p.add_argument(
        "--staging-layer",
        metavar="LAYER",
        action="append",
        dest="staging_layers",
        default=[],
        help="DXF layer containing staging lane positions.  "
             "Can be specified multiple times.",
    )
    p.add_argument(
        "--dock-layer",
        metavar="LAYER",
        action="append",
        dest="dock_layers",
        default=[],
        help="DXF layer containing dock door markers.  "
             "Can be specified multiple times.",
    )
    p.add_argument(
        "--frozen-layer",
        metavar="LAYER",
        action="append",
        dest="frozen_layers",
        default=[],
        help="DXF layer for frozen-temperature locations.  "
             "Locations on this layer receive TemperatureZone.FROZEN.",
    )
    p.add_argument(
        "--chilled-layer",
        metavar="LAYER",
        action="append",
        dest="chilled_layers",
        default=[],
        help="DXF layer for chilled-temperature locations.  "
             "Locations on this layer receive TemperatureZone.CHILLED.",
    )

    p.add_argument(
        "--attrib-tag",
        default="LOCID",
        metavar="TAG",
        help="Block attribute tag that holds the location ID string.  "
             "Default: LOCID.  Match the ATTDEF tag name in your DWG block.",
    )

    p.add_argument(
        "--unit",
        choices=["m", "ft", "feet", "inch", "inches", "mm", "cm"],
        default=None,
        help="Force a unit system.  If omitted, reads $INSUNITS from the DXF "
             "header (most CAD files include this).  Use this flag when the "
             "header is missing or wrong.",
    )

    p.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Write SQL output to this file.  If omitted, prints to stdout.",
    )

    p.add_argument(
        "--truncate",
        action="store_true",
        help="Prepend TRUNCATE statements to clear existing location/dock rows "
             "before inserting new ones.  Use with care on a live database.",
    )

    p.add_argument(
        "--dock-coords-py",
        metavar="FILE",
        default=None,
        help="Also write a Python dict literal for dock_door_coords to this "
             "file (useful for pasting into src/api/main.py).",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dxf_path = Path(args.dxf_file)
    floor_parser = FloorPlanParser()

    # ── list-layers mode ────────────────────────────────────────────────────
    if args.list_layers:
        try:
            layers = floor_parser.list_layers(dxf_path)
        except (ImportError, ValueError, FileNotFoundError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        print(f"Layers in {dxf_path.name} ({len(layers)} total):\n")
        for name in layers:
            print(f"  {name}")
        print(
            "\nRe-run with --locations-layer, --staging-layer, --dock-layer, "
            "etc. to specify which layers contain each feature type."
        )
        return 0

    # ── validate args ────────────────────────────────────────────────────────
    all_layers = (
        args.locations_layers
        + args.staging_layers
        + args.dock_layers
        + args.frozen_layers
        + args.chilled_layers
    )
    if not all_layers:
        print(
            "ERROR: No layers specified.  Run with --list-layers to see "
            "available layers, then add --locations-layer, --dock-layer, etc.",
            file=sys.stderr,
        )
        return 1

    config = LayerConfig(
        locations_layers=args.locations_layers,
        staging_layers=args.staging_layers,
        dock_layers=args.dock_layers,
        frozen_layers=args.frozen_layers,
        chilled_layers=args.chilled_layers,
        location_id_attrib_tag=args.attrib_tag,
        unit_override=args.unit,
    )

    # ── parse ────────────────────────────────────────────────────────────────
    try:
        result = floor_parser.parse(dxf_path, config)
    except (ImportError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Print summary to stderr so it doesn't pollute stdout SQL output
    print(f"Parsed {dxf_path.name}", file=sys.stderr)
    print(f"  Units      : {result.source_units}", file=sys.stderr)
    print(f"  Scale      : {result.unit_scale} → metres", file=sys.stderr)
    print(f"  Locations  : {len(result.locations)}", file=sys.stderr)
    print(f"  Dock doors : {len(result.dock_doors)}", file=sys.stderr)

    if result.warnings:
        print("\nWarnings:", file=sys.stderr)
        for w in result.warnings:
            print(f"  ⚠  {w}", file=sys.stderr)

    if result.dock_doors:
        print("\nDock doors:", file=sys.stderr)
        for d in result.dock_doors:
            print(f"  Door {d.door_id}: ({d.x}, {d.y}) m", file=sys.stderr)

    # ── generate SQL ─────────────────────────────────────────────────────────
    sql = generate_sql(result, clear_existing=args.truncate)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(sql, encoding="utf-8")
        print(f"\nSQL written to {out_path}", file=sys.stderr)
    else:
        print("\n" + sql)

    # ── optional Python dict ─────────────────────────────────────────────────
    if args.dock_coords_py:
        py_path = Path(args.dock_coords_py)
        py_snippet = generate_dock_coords_python(result)
        py_path.write_text(py_snippet, encoding="utf-8")
        print(f"dock_door_coords dict written to {py_path}", file=sys.stderr)

    if result.warnings:
        return 2  # partial success
    return 0


if __name__ == "__main__":
    sys.exit(main())
