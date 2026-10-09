"""Inventory domain models: locations, SKUs, and inventory positions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class TemperatureZone(str, Enum):
    """Temperature zone classification for locations and SKUs."""

    AMBIENT = "AMBIENT"
    CHILLED = "CHILLED"
    FROZEN = "FROZEN"


class HazmatClass(str, Enum):
    """DOT hazardous materials classification."""

    CLASS_1 = "1"
    CLASS_2 = "2"
    CLASS_3 = "3"
    CLASS_4 = "4"
    CLASS_5_1 = "5.1"
    CLASS_5_2 = "5.2"
    CLASS_6 = "6"
    CLASS_7 = "7"
    CLASS_8 = "8"
    CLASS_9 = "9"


class ABCClass(str, Enum):
    """ABC inventory classification based on velocity/value."""

    A = "A"
    B = "B"
    C = "C"


class Location(BaseModel):
    """A physical warehouse location with coordinates and constraints.

    Args:
        location_id: Unique identifier for the location.
        zone: Zone label (e.g. 'A', 'B', 'STAGING').
        aisle: Aisle number.
        bay: Bay number within aisle.
        level: Vertical level (0 = floor).
        x: X coordinate in meters from warehouse origin.
        y: Y coordinate in meters from warehouse origin.
        temperature_zone: Required temperature zone.
        max_weight_kg: Maximum weight capacity in kilograms.
        max_volume_m3: Maximum volume capacity in cubic meters.
        is_staging: Whether this is a staging location near a dock.
        nearest_dock_door: Dock door number this staging spot serves.
    """

    model_config = ConfigDict(from_attributes=True)

    location_id: str
    zone: str
    aisle: int
    bay: int
    level: int
    x: float
    y: float
    temperature_zone: TemperatureZone = TemperatureZone.AMBIENT
    max_weight_kg: float = 2000.0
    max_volume_m3: float = 10.0
    is_staging: bool = False
    nearest_dock_door: int | None = None


class SKU(BaseModel):
    """A stock-keeping unit with physical and regulatory attributes.

    Args:
        sku_id: Unique SKU identifier.
        description: Human-readable description.
        weight_kg: Weight per unit in kilograms.
        volume_m3: Volume per unit in cubic meters.
        hazmat_class: DOT hazmat classification, if applicable.
        requires_temperature_zone: Required storage temperature zone.
        abc_class: ABC inventory classification.
    """

    model_config = ConfigDict(from_attributes=True)

    sku_id: str
    description: str
    weight_kg: float
    volume_m3: float
    hazmat_class: HazmatClass | None = None
    requires_temperature_zone: TemperatureZone = TemperatureZone.AMBIENT
    abc_class: ABCClass = ABCClass.C


class InventoryPosition(BaseModel):
    """A quantity of a SKU stored at a specific location.

    Args:
        position_id: Unique identifier for this inventory position.
        sku: The SKU stored at this position.
        location: The physical location of this position.
        quantity: Number of units.
        lot_number: Optional lot/batch number for traceability.
        expiry_date: Optional expiry date for perishable goods.
    """

    model_config = ConfigDict(from_attributes=True)

    position_id: str
    sku: SKU
    location: Location
    quantity: int
    lot_number: str | None = None
    expiry_date: datetime | None = None
