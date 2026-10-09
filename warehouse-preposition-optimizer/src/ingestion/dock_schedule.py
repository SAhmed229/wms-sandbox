"""Dock schedule ingestion: retrieve and filter carrier appointments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.ingestion.wms_adapter import WMSAdapter
from src.models.orders import AppointmentStatus, CarrierAppointment


class DockScheduleIngester:
    """Retrieves and filters carrier appointments from the WMS.

    Args:
        adapter: WMS adapter to query for appointment data.
    """

    def __init__(self, adapter: WMSAdapter) -> None:
        self._adapter = adapter

    async def get_active_appointments(
        self,
        horizon_hours: float = 24,
        exclude_statuses: set[AppointmentStatus] | None = None,
    ) -> list[CarrierAppointment]:
        """Return active appointments within the time window, sorted by arrival.

        Filters out DEPARTED appointments by default, as those trucks have left.

        Args:
            horizon_hours: How many hours ahead to look.
            exclude_statuses: Statuses to exclude. Defaults to {DEPARTED}.

        Returns:
            Appointments sorted by scheduled_arrival ascending.
        """
        if exclude_statuses is None:
            exclude_statuses = {AppointmentStatus.DEPARTED}

        all_appointments = await self._adapter.get_carrier_appointments(horizon_hours)
        now = datetime.now(UTC)
        cutoff = now + timedelta(hours=horizon_hours)

        active = [
            appt
            for appt in all_appointments
            if appt.status not in exclude_statuses
            and appt.scheduled_arrival <= cutoff
        ]

        return sorted(active, key=lambda a: a.scheduled_arrival)

    async def get_appointments_for_door(
        self, dock_door: int, horizon_hours: float = 24
    ) -> list[CarrierAppointment]:
        """Return active appointments for a specific dock door.

        Args:
            dock_door: The dock door number to filter by.
            horizon_hours: How many hours ahead to look.

        Returns:
            Appointments for this dock door, sorted by scheduled_arrival.
        """
        active = await self.get_active_appointments(horizon_hours)
        return [a for a in active if a.dock_door == dock_door]
