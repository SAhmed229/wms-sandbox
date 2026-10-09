"""DXF/DWG warehouse floor plan parser.

Reads AutoCAD DXF files and extracts rack locations, staging lanes, and dock
doors as structured data suitable for seeding the ``locations`` and
``dock_doors`` tables.

DWG vs DXF
----------
DWG is Autodesk's proprietary binary format.  ``ezdxf`` reads DXF natively
but cannot read DWG directly.  Export DWG → DXF first:

* **AutoCAD / AutoCAD LT**: ``SAVEAS`` → choose "AutoCAD 2018 DXF" (R2018).
* **ODA File Converter** (free, no install required):
  https://www.opendesign.com/guestfiles/oda_file_converter
* **LibreCAD** (open-source): File → Export → DXF.
* **DraftSight** (free tier): File → Save As → DXF.

Layer conventions
-----------------
This parser is layer-driven.  You tell it which DXF layers contain each
feature type via :class:`LayerConfig`.  Common layer names in warehouse DWGs:

* ``RACK_POSITIONS``, ``STORAGE_LOCS`` → bulk storage locations
* ``STAGING_LANES``, ``DOCK_STAGING`` → staging locations near doors
* ``DOCK_DOORS``, ``LOADING_DOCKS`` → dock door markers
* ``COLD_STORAGE``, ``FREEZER`` → frozen/chilled locations

Run the CLI with ``--list-layers`` to discover the layer names in your file
before specifying them:

    uv run python scripts/import_floor_plan.py layout.dxf --list-layers

Entity extraction strategy
---------------------------
For each entity on a recognised layer the parser tries, in order:

1. ``INSERT`` (block reference) → insertion point = location centroid;
   block ``ATTRIB`` with ``location_id_attrib_tag`` → location ID.
2. ``POINT`` → location coordinates.
3. ``CIRCLE`` / ``ARC`` → centre point.
4. ``LWPOLYLINE`` / ``POLYLINE`` → bounding-box centroid.
5. ``TEXT`` / ``MTEXT`` → insertion point; text content → location ID.

Location IDs are auto-generated as ``A{aisle:02d}-B{bay:03d}`` (or
``STAGE-{n:03d}`` for staging rows) when no attribute value is found.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.models.inventory import TemperatureZone

if TYPE_CHECKING:
    pass  # ezdxf imported lazily to keep module loadable without the package

# ---------------------------------------------------------------------------
# DXF INSUNITS → metres conversion table
# ---------------------------------------------------------------------------
_INSUNITS_TO_METRES: dict[int, float] = {
    0: 1.0,      # Unitless → assume metres
    1: 0.0254,   # Inches
    2: 0.3048,   # Feet
    3: 1609.344, # Miles
    4: 0.001,    # Millimetres
    5: 0.01,     # Centimetres
    6: 1.0,      # Metres
    7: 1000.0,   # Kilometres
    14: 1e-10,   # Angstroms
    15: 1e-9,    # Nanometres
    16: 1e-6,    # Microns (μm)
    17: 0.1,     # Decimetres
    18: 10.0,    # Decametres
    19: 100.0,   # Hectometres
}

_INSUNITS_NAMES: dict[int, str] = {
    0: "unitless (assumed metres)",
    1: "inches",
    2: "feet",
    4: "millimetres",
    5: "centimetres",
    6: "metres",
}

_UNIT_OVERRIDE_MAP: dict[str, float] = {
    "m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "mm": 0.001, "millimetre": 0.001, "millimetres": 0.001,
    "millimeter": 0.001, "millimeters": 0.001,
    "cm": 0.01, "centimetre": 0.01, "centimetres": 0.01,
    "centimeter": 0.01, "centimeters": 0.01,
}


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class LayerConfig:
    """Maps DXF layer names to warehouse feature types.

    Args:
        locations_layers: Layers containing bulk storage rack positions.
        staging_layers: Layers containing staging lane positions.
        dock_layers: Layers containing dock door markers.
        frozen_layers: Layers whose locations get ``TemperatureZone.FROZEN``.
        chilled_layers: Layers whose locations get ``TemperatureZone.CHILLED``.
        location_id_attrib_tag: Block attribute tag name that holds the
            location identifier string (case-insensitive match).
        unit_override: Force a specific unit system.  Accepted values:
            ``"m"``, ``"ft"``, ``"inch"``, ``"mm"``, ``"cm"``.
            Overrides the ``$INSUNITS`` header variable in the file.
    """

    locations_layers: list[str] = field(default_factory=list)
    staging_layers: list[str] = field(default_factory=list)
    dock_layers: list[str] = field(default_factory=list)
    frozen_layers: list[str] = field(default_factory=list)
    chilled_layers: list[str] = field(default_factory=list)
    location_id_attrib_tag: str = "LOCID"
    unit_override: str | None = None


@dataclass
class ParsedLocation:
    """A warehouse location extracted from a DXF drawing.

    All coordinates are in metres after unit conversion.
    """

    location_id: str
    x: float
    y: float
    temperature_zone: TemperatureZone
    is_staging: bool
    nearest_dock_door: int | None
    source_layer: str
    aisle: int
    bay: int


@dataclass
class ParsedDockDoor:
    """A dock door extracted from a DXF drawing."""

    door_id: int
    x: float
    y: float


@dataclass
class FloorPlanResult:
    """Result of parsing a DXF warehouse floor plan.

    Args:
        locations: All extracted storage and staging locations.
        dock_doors: All extracted dock doors, sorted by x-coordinate.
        unit_scale: Scale factor applied to raw DXF coordinates → metres.
        source_units: Human-readable name of the detected/overridden unit.
        warnings: Non-fatal messages about missing layers or empty results.
    """

    locations: list[ParsedLocation]
    dock_doors: list[ParsedDockDoor]
    unit_scale: float
    source_units: str
    warnings: list[str]


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class FloorPlanParser:
    """Parses DXF warehouse floor plans into structured location records.

    Typical usage::

        parser = FloorPlanParser()

        # Discover layer names first
        layers = parser.list_layers("warehouse.dxf")
        print(layers)

        # Configure and parse
        config = LayerConfig(
            locations_layers=["RACK_POSITIONS"],
            staging_layers=["STAGING_LANES"],
            dock_layers=["DOCK_DOORS"],
            frozen_layers=["FREEZER_ZONE"],
        )
        result = parser.parse("warehouse.dxf", config)
    """

    def list_layers(self, file_path: str | Path) -> list[str]:
        """Return all layer names present in the DXF file, sorted alphabetically.

        Args:
            file_path: Path to a ``.dxf`` file.

        Returns:
            Sorted list of layer name strings.

        Raises:
            ImportError: If ``ezdxf`` is not installed.
            ValueError: If ``file_path`` has a ``.dwg`` extension.
            FileNotFoundError: If the file does not exist.
        """
        _require_ezdxf()
        doc = _open_dxf(Path(file_path))
        return sorted(layer.dxf.name for layer in doc.layers)

    def parse(self, file_path: str | Path, config: LayerConfig) -> FloorPlanResult:
        """Parse a DXF file and extract warehouse features.

        Args:
            file_path: Path to a ``.dxf`` file.
            config: Layer-to-feature mapping configuration.

        Returns:
            :class:`FloorPlanResult` with all extracted entities.

        Raises:
            ImportError: If ``ezdxf`` is not installed.
            ValueError: If ``file_path`` has a ``.dwg`` extension.
            FileNotFoundError: If the file does not exist.
        """
        _require_ezdxf()
        doc = _open_dxf(Path(file_path))

        unit_scale, source_units = _resolve_units(doc, config.unit_override)
        warnings: list[str] = []

        msp = doc.modelspace()

        # Upper-case sets for O(1) lookup
        loc_set = {la.upper() for la in config.locations_layers}
        staging_set = {la.upper() for la in config.staging_layers}
        dock_set = {la.upper() for la in config.dock_layers}
        frozen_set = {la.upper() for la in config.frozen_layers}
        chilled_set = {la.upper() for la in config.chilled_layers}

        # All feature layers (temperature layers can overlap with loc/staging)
        all_feature_layers = loc_set | staging_set | dock_set | frozen_set | chilled_set

        raw_locs: list[_RawLoc] = []
        raw_docks: list[tuple[float, float]] = []

        for entity in msp:
            layer_name = getattr(entity.dxf, "layer", "0")
            layer_upper = layer_name.upper()

            if layer_upper not in all_feature_layers:
                continue

            cx, cy = _entity_centroid(entity)
            if cx is None or cy is None:
                continue

            sx, sy = cx * unit_scale, cy * unit_scale

            if layer_upper in dock_set:
                raw_docks.append((sx, sy))
                continue

            is_staging = layer_upper in staging_set
            # Temperature zone: frozen/chilled layers override ambient
            if layer_upper in frozen_set:
                tzone = TemperatureZone.FROZEN
            elif layer_upper in chilled_set:
                tzone = TemperatureZone.CHILLED
            else:
                tzone = TemperatureZone.AMBIENT

            loc_id = _extract_location_id(entity, config.location_id_attrib_tag)

            raw_locs.append(_RawLoc(
                x=round(sx, 4),
                y=round(sy, 4),
                loc_id=loc_id or "",
                is_staging=is_staging,
                tzone=tzone,
                layer=layer_name,
            ))

        # Deduplicate entities that map to the same physical point
        raw_locs = _deduplicate_locs(raw_locs, tolerance=0.1)

        # Build dock doors (sorted left-to-right by x)
        dock_doors = [
            ParsedDockDoor(door_id=i + 1, x=round(x, 3), y=round(y, 3))
            for i, (x, y) in enumerate(sorted(raw_docks, key=lambda p: p[0]))
        ]
        if not dock_doors:
            warnings.append(
                f"No dock door entities found on layers {config.dock_layers!r}. "
                "Use --list-layers to confirm the correct layer names."
            )

        locations = _build_locations(raw_locs, dock_doors, warnings)

        if not locations:
            warnings.append(
                f"No storage location entities found on layers "
                f"{config.locations_layers!r} or {config.staging_layers!r}."
            )

        return FloorPlanResult(
            locations=locations,
            dock_doors=dock_doors,
            unit_scale=unit_scale,
            source_units=source_units,
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# SQL and config generation helpers
# ---------------------------------------------------------------------------

def generate_sql(result: FloorPlanResult, clear_existing: bool = False) -> str:
    """Generate SQL INSERT statements from a :class:`FloorPlanResult`.

    The output is compatible with the ``locations`` and ``dock_doors`` table
    schema in ``scripts/init_db.sql``.

    Args:
        result: Parsed floor plan.
        clear_existing: If True, prepend ``TRUNCATE`` statements that wipe
            existing seed rows before inserting new ones.

    Returns:
        SQL string ready to execute or append to ``init_db.sql``.
    """
    lines: list[str] = [
        "-- ── Generated by scripts/import_floor_plan.py ──────────────────────────────",
        f"-- Source units : {result.source_units} (scale factor to metres: {result.unit_scale})",
        f"-- Locations    : {len(result.locations)}",
        f"-- Dock doors   : {len(result.dock_doors)}",
        "",
    ]

    if clear_existing:
        lines += [
            "TRUNCATE TABLE dock_doors CASCADE;",
            "TRUNCATE TABLE locations CASCADE;",
            "",
        ]

    if result.dock_doors:
        lines.append("-- Dock doors")
        lines.append(
            "INSERT INTO dock_doors (door_id, x, y) VALUES"
        )
        door_rows = [
            f"    ({d.door_id}, {d.x}, {d.y})"
            for d in result.dock_doors
        ]
        lines.append(",\n".join(door_rows) + ";")
        lines.append("")

    if result.locations:
        lines.append("-- Storage and staging locations")
        lines.append(
            "INSERT INTO locations"
            " (location_id, zone, aisle, bay, level, x, y,"
            " temperature_zone, max_weight_kg, max_volume_m3,"
            " is_staging, nearest_dock_door)"
            " VALUES"
        )
        loc_rows: list[str] = []
        for loc in result.locations:
            zone = "STAGING" if loc.is_staging else _zone_label(loc.aisle)
            door = str(loc.nearest_dock_door) if loc.nearest_dock_door is not None else "NULL"
            loc_rows.append(
                f"    ('{loc.location_id}', '{zone}', {loc.aisle}, {loc.bay},"
                f" 0, {loc.x}, {loc.y}, '{loc.temperature_zone.value}',"
                f" 2000.0, 10.0, {'TRUE' if loc.is_staging else 'FALSE'}, {door})"
            )
        lines.append(",\n".join(loc_rows) + ";")

    return "\n".join(lines) + "\n"


def generate_dock_coords_python(result: FloorPlanResult) -> str:
    """Return a Python dict literal for ``MovementScorer(dock_door_coords=...)``.

    Args:
        result: Parsed floor plan.

    Returns:
        Python source snippet (not a complete file).
    """
    if not result.dock_doors:
        return "dock_door_coords = {}  # No dock doors parsed\n"
    entries = ", ".join(
        f"{d.door_id}: ({d.x}, {d.y})" for d in result.dock_doors
    )
    return f"dock_door_coords = {{{entries}}}\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@dataclass
class _RawLoc:
    x: float
    y: float
    loc_id: str
    is_staging: bool
    tzone: TemperatureZone
    layer: str


def _require_ezdxf() -> None:
    try:
        import ezdxf  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "ezdxf is required for floor plan import. "
            "Install it with: uv add ezdxf  or  pip install ezdxf"
        ) from exc


def _open_dxf(path: Path):  # type: ignore[return]
    """Open a DXF file with ezdxf.  Raises ValueError for .dwg files."""
    import ezdxf
    from ezdxf import recover

    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if path.suffix.lower() == ".dwg":
        raise ValueError(
            f"{path.name} is a DWG file.  ezdxf cannot read DWG binary format directly.\n"
            "\n"
            "Convert to DXF first using one of these free tools:\n"
            "  • AutoCAD / AutoCAD LT : SAVEAS → AutoCAD 2018 DXF\n"
            "  • ODA File Converter   : https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "  • LibreCAD             : File → Export → DXF\n"
            "  • DraftSight (free)    : File → Save As → DXF R2018\n"
        )

    try:
        doc, auditor = recover.readfile(str(path))
        return doc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Failed to open {path.name}: {exc}") from exc


def _resolve_units(doc, override: str | None) -> tuple[float, str]:
    """Return (scale_to_metres, human_readable_name) for the DXF document."""
    if override:
        key = override.lower().strip()
        if key not in _UNIT_OVERRIDE_MAP:
            raise ValueError(
                f"Unknown unit override {override!r}. "
                f"Choose from: {', '.join(sorted(_UNIT_OVERRIDE_MAP))}"
            )
        return _UNIT_OVERRIDE_MAP[key], f"{override} (override)"

    try:
        insunits = int(doc.header.get("$INSUNITS", 0))
    except Exception:  # noqa: BLE001
        insunits = 0

    scale = _INSUNITS_TO_METRES.get(insunits, 1.0)
    name = _INSUNITS_NAMES.get(insunits, f"INSUNITS={insunits}")
    return scale, name


def _entity_centroid(entity) -> tuple[float | None, float | None]:
    """Return the (x, y) representative point for a DXF entity."""
    dtype = entity.dxftype()

    if dtype == "INSERT":
        pt = entity.dxf.insert
        return float(pt.x), float(pt.y)

    if dtype == "POINT":
        pt = entity.dxf.location
        return float(pt.x), float(pt.y)

    if dtype in ("CIRCLE", "ARC"):
        pt = entity.dxf.center
        return float(pt.x), float(pt.y)

    if dtype == "TEXT":
        pt = entity.dxf.insert
        return float(pt.x), float(pt.y)

    if dtype == "MTEXT":
        pt = entity.dxf.insert
        return float(pt.x), float(pt.y)

    if dtype == "LWPOLYLINE":
        pts = list(entity.get_points())
        if pts:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            return sum(xs) / len(xs), sum(ys) / len(ys)
        return None, None

    if dtype == "POLYLINE":
        verts = list(entity.vertices)
        if verts:
            xs = [float(v.dxf.location.x) for v in verts]
            ys = [float(v.dxf.location.y) for v in verts]
            return sum(xs) / len(xs), sum(ys) / len(ys)
        return None, None

    if dtype == "LINE":
        s = entity.dxf.start
        e = entity.dxf.end
        return (float(s.x) + float(e.x)) / 2, (float(s.y) + float(e.y)) / 2

    return None, None


def _extract_location_id(entity, attrib_tag: str) -> str | None:
    """Extract a location ID from block attributes or text content."""
    dtype = entity.dxftype()

    if dtype == "INSERT":
        tag_upper = attrib_tag.upper()
        for attrib in entity.attribs:
            if attrib.dxf.tag.upper() == tag_upper:
                val = attrib.dxf.text.strip()
                return val if val else None
        # Fallback: return first non-empty attribute value
        for attrib in entity.attribs:
            val = attrib.dxf.text.strip()
            if val:
                return val
        return None

    if dtype == "TEXT":
        val = entity.dxf.text.strip()
        return val if val else None

    if dtype == "MTEXT":
        # Strip ezdxf MTEXT formatting codes
        raw = entity.plain_mtext().strip()
        return raw if raw else None

    return None


def _deduplicate_locs(locs: list[_RawLoc], tolerance: float) -> list[_RawLoc]:
    """Remove duplicate entities whose centroids are within *tolerance* metres."""
    kept: list[_RawLoc] = []
    for loc in locs:
        if not any(
            math.hypot(loc.x - k.x, loc.y - k.y) < tolerance
            for k in kept
        ):
            kept.append(loc)
    return kept


def _nearest_dock(
    x: float, y: float, docks: list[ParsedDockDoor]
) -> tuple[int | None, float]:
    """Return (door_id, distance_metres) of the nearest dock door."""
    if not docks:
        return None, float("inf")
    best = min(docks, key=lambda d: math.hypot(x - d.x, y - d.y))
    return best.door_id, math.hypot(x - best.x, y - best.y)


def _group_by_tolerance(values: list[float], tolerance: float) -> dict[float, int]:
    """Map each value to a 1-based group index by clustering within tolerance."""
    groups: list[float] = []
    result: dict[float, int] = {}
    for v in sorted(set(values)):
        matched = next(
            (g for g in groups if abs(v - g) <= tolerance), None
        )
        if matched is None:
            groups.append(v)
            matched = v
        result[v] = groups.index(matched) + 1
    return result


def _build_locations(
    raw: list[_RawLoc],
    docks: list[ParsedDockDoor],
    warnings: list[str],
) -> list[ParsedLocation]:
    """Assign aisle/bay numbers, nearest dock door, and auto IDs."""
    if not raw:
        return []

    # Sort by y (south→north = aisle order) then x (west→east = bay order)
    raw_sorted = sorted(raw, key=lambda r: (r.y, r.x))

    # Cluster y-values into aisles (0.5 m tolerance)
    y_to_aisle = _group_by_tolerance([r.y for r in raw_sorted], tolerance=0.5)

    # Group by aisle to assign bay numbers
    aisle_members: dict[int, list[_RawLoc]] = defaultdict(list)
    for r in raw_sorted:
        aisle_members[y_to_aisle[r.y]].append(r)

    locations: list[ParsedLocation] = []
    id_counter: dict[str, int] = defaultdict(int)

    for aisle_idx in sorted(aisle_members):
        members = sorted(aisle_members[aisle_idx], key=lambda r: r.x)
        for bay_idx, r in enumerate(members, start=1):
            nearest_door, _ = _nearest_dock(r.x, r.y, docks)

            if r.loc_id:
                loc_id = r.loc_id
            else:
                if r.is_staging:
                    prefix = "STAGE"
                else:
                    prefix = f"A{aisle_idx:02d}"
                id_counter[prefix] += 1
                loc_id = f"{prefix}-B{id_counter[prefix]:03d}"

            locations.append(ParsedLocation(
                location_id=loc_id,
                x=r.x,
                y=r.y,
                temperature_zone=r.tzone,
                is_staging=r.is_staging,
                nearest_dock_door=nearest_door,
                source_layer=r.layer,
                aisle=aisle_idx,
                bay=bay_idx,
            ))

    return locations


def _zone_label(aisle: int) -> str:
    """Assign a zone label based on aisle index (distance from dock)."""
    if aisle <= 3:
        return "A"
    if aisle <= 6:
        return "B"
    return "C"
