"""Order and appointment domain models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class AppointmentStatus(str, Enum):
    """Lifecycle status of a carrier appointment."""

    SCHEDULED = "SCHEDULED"
    CHECKED_IN = "CHECKED_IN"
    LOADING = "LOADING"
    DEPARTED = "DEPARTED"


class CarrierAppointment(BaseModel):
    """A scheduled carrier pickup appointment at a dock door.

    Args:
        appointment_id: Unique identifier for the appointment.
        carrier: Carrier name or SCAC code.
        dock_door: Dock door number assigned to this appointment.
        scheduled_arrival: Expected arrival time.
        scheduled_departure: Expected departure time.
        status: Current lifecycle status.
    """

    model_config = ConfigDict(from_attributes=True)

    appointment_id: str
    carrier: str
    dock_door: int
    scheduled_arrival: datetime
    scheduled_departure: datetime
    status: AppointmentStatus = AppointmentStatus.SCHEDULED


class OrderLine(BaseModel):
    """A single line item on an outbound order.

    Args:
        line_id: Unique line identifier.
        sku_id: SKU being ordered.
        quantity: Units required.
        picked: Whether this line has been picked.
    """

    model_config = ConfigDict(from_attributes=True)

    line_id: str
    sku_id: str
    quantity: int
    picked: bool = False


class OutboundOrder(BaseModel):
    """An outbound shipment order linked to a carrier appointment.

    Args:
        order_id: Unique order identifier.
        appointment: The carrier appointment this order ships on.
        lines: List of order line items.
        priority: Order priority from 1 (lowest) to 10 (highest).
        cutoff_time: Latest time by which the order must be staged.
    """

    model_config = ConfigDict(from_attributes=True)

    order_id: str
    appointment: CarrierAppointment
    lines: list[OrderLine]
    priority: int
    cutoff_time: datetime
