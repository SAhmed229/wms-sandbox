"""Abstract WMS adapter interface and WarehouseState bundle."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.models.inventory import InventoryPosition, Location
from src.models.orders import CarrierAppointment, OutboundOrder


@dataclass
class WarehouseState:
    """Snapshot of the current warehouse state used for scoring.

    Args:
        inventory_positions: All active inventory positions.
        outbound_orders: Outbound orders within the planning horizon.
        appointments: Carrier appointments within the planning horizon.
        staging_locations: Available staging locations near dock doors.
        resource_utilization: Map of resource_id -> utilization [0.0, 1.0].
        location_utilization: Map of location_id -> fill fraction [0.0, 1.0].
    """

    inventory_positions: list[InventoryPosition] = field(default_factory=list)
    outbound_orders: list[OutboundOrder] = field(default_factory=list)
    appointments: list[CarrierAppointment] = field(default_factory=list)
    staging_locations: list[Location] = field(default_factory=list)
    resource_utilization: dict[str, float] = field(default_factory=dict)
    location_utilization: dict[str, float] = field(default_factory=dict)


class WMSAdapter(ABC):
    """Abstract base class for WMS data ingestion.

    All WMS interaction goes through this interface. Implementations
    may connect to SQL databases, REST APIs, or message queues.
    The system is read-only — no writes to WMS data.
    """

    @abstractmethod
    async def get_inventory_positions(
        self, zone: str | None = None
    ) -> list[InventoryPosition]:
        """Fetch current inventory positions, optionally filtered by zone.

        Args:
            zone: Zone label to filter by. None returns all zones.

        Returns:
            List of inventory positions.
        """

    @abstractmethod
    async def get_outbound_orders(
        self, horizon_hours: float = 24
    ) -> list[OutboundOrder]:
        """Fetch outbound orders with cutoff within the planning horizon.

        Args:
            horizon_hours: Number of hours ahead to look for orders.

        Returns:
            List of outbound orders sorted by cutoff time ascending.
        """

    @abstractmethod
    async def get_carrier_appointments(
        self, horizon_hours: float = 24
    ) -> list[CarrierAppointment]:
        """Fetch carrier appointments within the planning horizon.

        Args:
            horizon_hours: Number of hours ahead to look for appointments.

        Returns:
            List of carrier appointments sorted by scheduled_arrival ascending.
        """

    @abstractmethod
    async def get_staging_locations(
        self, dock_door: int | None = None
    ) -> list[Location]:
        """Fetch staging locations near dock doors.

        Args:
            dock_door: Filter to locations serving this dock door. None returns all.

        Returns:
            List of staging locations.
        """

    @abstractmethod
    async def get_location_utilization(self) -> dict[str, float]:
        """Fetch current fill fraction for all locations.

        Returns:
            Map of location_id to utilization fraction [0.0, 1.0].
        """

    async def get_warehouse_state(
        self, horizon_hours: float = 24
    ) -> WarehouseState:
        """Fetch and bundle all warehouse state into a single snapshot.

        Args:
            horizon_hours: Planning horizon for orders and appointments.

        Returns:
            WarehouseState with all current data.
        """
        inventory_positions = await self.get_inventory_positions()
        outbound_orders = await self.get_outbound_orders(horizon_hours)
        appointments = await self.get_carrier_appointments(horizon_hours)
        staging_locations = await self.get_staging_locations()
        location_utilization = await self.get_location_utilization()

        return WarehouseState(
            inventory_positions=inventory_positions,
            outbound_orders=outbound_orders,
            appointments=appointments,
            staging_locations=staging_locations,
            resource_utilization={},
            location_utilization=location_utilization,
        )
