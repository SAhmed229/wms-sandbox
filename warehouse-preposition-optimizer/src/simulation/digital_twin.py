"""SimPy discrete-event simulation of warehouse operations."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Generator

import simpy

from src.models.inventory import InventoryPosition, Location
from src.models.orders import CarrierAppointment, OutboundOrder
from src.simulation.reward import EpisodeMetrics


@dataclass
class SimMovement:
    """A repositioning movement in the simulation.

    Args:
        sku_id: SKU being moved.
        from_location: Origin location.
        to_location: Destination staging location.
        distance_meters: Manhattan distance of the move.
        score: V(m) score used to prioritise the move.
    """

    sku_id: str
    from_location: Location
    to_location: Location
    distance_meters: float
    score: float = 0.0


@dataclass
class SimTruck:
    """A truck arriving to load at a dock door.

    Args:
        appointment: The carrier appointment.
        orders: Outbound orders assigned to this truck.
        arrival_time: Simulated arrival time (seconds from shift start).
        departure_time: Actual departure time (filled in by simulation).
        scheduled_departure: Scheduled departure time (seconds from shift start).
    """

    appointment: CarrierAppointment
    orders: list[OutboundOrder]
    arrival_time: float
    scheduled_departure: float
    departure_time: float = 0.0
    total_loading_seconds: float = 0.0
    loads_from_staging: int = 0
    loads_from_storage: int = 0


@dataclass
class SimConfig:
    """Configuration for the SimPy warehouse simulation.

    Args:
        shift_duration_seconds: Length of one episode in seconds (default: 8h).
        forklift_count: Number of forklifts / AGVs in the simulation.
        forklift_speed_mps: Average travel speed in metres per second.
        handling_time_seconds: Fixed pick+place time per pallet.
        loading_time_per_pallet_seconds: Time to load one pallet onto a truck.
        staging_loading_speedup: Multiplier applied to loading time when SKU is
            already staged near the dock. Values < 1.0 make staged loads faster.
        order_inter_arrival_mean_seconds: Mean time between new order arrivals
            (for stochastic demand; set to 0 to use only pre-seeded orders).
        random_seed: RNG seed for reproducibility.
    """

    shift_duration_seconds: float = 28_800.0  # 8 hours
    forklift_count: int = 3
    forklift_speed_mps: float = 2.2
    handling_time_seconds: float = 45.0
    loading_time_per_pallet_seconds: float = 60.0
    staging_loading_speedup: float = 0.6
    order_inter_arrival_mean_seconds: float = 0.0
    random_seed: int = 42


class WarehouseDigitalTwin:
    """SimPy discrete-event simulation of a warehouse shift.

    Models forklifts as shared resources, trucks as processes that arrive,
    load from staging/storage, and depart. Pre-positioned inventory reduces
    loading time; the difference is tracked as seconds_saved.

    Args:
        config: Simulation parameters.
        inventory: Initial inventory positions.
        appointments: Carrier appointments for the shift.
        orders: Outbound orders for the shift.
        pending_movements: Pre-positioning movements to execute early in the shift.
    """

    def __init__(
        self,
        config: SimConfig,
        inventory: list[InventoryPosition],
        appointments: list[CarrierAppointment],
        orders: list[OutboundOrder],
        pending_movements: list[SimMovement] | None = None,
    ) -> None:
        self._config = config
        self._appointments = appointments
        self._orders = orders
        self._pending_movements = pending_movements or []

        # Build mutable inventory index: sku_id → position
        self._inventory: dict[str, InventoryPosition] = {
            pos.sku.sku_id: pos for pos in inventory
        }

        self._env = simpy.Environment()
        self._forklifts = simpy.Resource(self._env, capacity=config.forklift_count)
        self._rng = random.Random(config.random_seed)

        self.metrics = EpisodeMetrics()
        self._trucks: list[SimTruck] = []
        self._events: list[dict[str, Any]] = []  # structured log of simulation events

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> EpisodeMetrics:
        """Run the simulation for one full shift.

        Returns:
            EpisodeMetrics populated with results.
        """
        self._env = simpy.Environment()
        self._forklifts = simpy.Resource(self._env, capacity=self._config.forklift_count)
        self.metrics = EpisodeMetrics()
        self._trucks = []
        self._events = []

        # Schedule pre-positioning movements (executed at sim-time 0 onwards)
        for movement in self._pending_movements:
            self._env.process(self._execute_movement(movement))

        # Schedule truck arrivals
        for appt in self._appointments:
            arrival_s = _appointment_offset(appt, self._config.shift_duration_seconds)
            depart_s = _departure_offset(appt, self._config.shift_duration_seconds)
            orders_for_truck = [o for o in self._orders if o.appointment.appointment_id == appt.appointment_id]
            truck = SimTruck(
                appointment=appt,
                orders=orders_for_truck,
                arrival_time=arrival_s,
                scheduled_departure=depart_s,
            )
            self._trucks.append(truck)
            self._env.process(self._truck_process(truck))

        # Optional stochastic order arrivals
        if self._config.order_inter_arrival_mean_seconds > 0:
            self._env.process(self._order_arrival_process())

        self._env.run(until=self._config.shift_duration_seconds)

        self._compute_summary_metrics()
        return self.metrics

    def get_avg_distance_to_dock(self, dock_door: int) -> float:
        """Return average Manhattan distance from ordered SKUs to a dock door.

        Used for potential-based shaping reward computation.

        Args:
            dock_door: Dock door number to measure distance to.

        Returns:
            Mean distance in metres, or 0.0 if no matching inventory.
        """
        sku_ids = {
            line.sku_id
            for order in self._orders
            if order.appointment.dock_door == dock_door
            for line in order.lines
        }
        distances: list[float] = []
        for sku_id in sku_ids:
            pos = self._inventory.get(sku_id)
            if pos is not None:
                distances.append(
                    abs(pos.location.x) + abs(pos.location.y - float(dock_door) * 5.0)
                )
        return sum(distances) / len(distances) if distances else 0.0

    def apply_movement(self, movement: SimMovement) -> None:
        """Apply a movement immediately to the internal inventory index.

        Called by the Gymnasium environment to reflect agent actions in the
        digital twin state before the next simulation step.

        Args:
            movement: The movement to apply.
        """
        pos = self._inventory.get(movement.sku_id)
        if pos is None:
            return
        updated = pos.model_copy(update={"location": movement.to_location})
        self._inventory[movement.sku_id] = updated

    # ------------------------------------------------------------------
    # SimPy process generators
    # ------------------------------------------------------------------

    def _execute_movement(self, movement: SimMovement) -> Generator[Any, Any, None]:
        """SimPy process: acquire forklift, travel, pick, travel back, place.

        Args:
            movement: The SimMovement to execute.
        """
        with self._forklifts.request() as req:
            yield req
            travel_time = movement.distance_meters / self._config.forklift_speed_mps
            total_time = travel_time + self._config.handling_time_seconds
            yield self._env.timeout(total_time)

            self.apply_movement(movement)
            self.metrics.movements_executed += 1
            self.metrics.total_movement_cost_seconds += total_time
            self._log_event("movement_complete", sku_id=movement.sku_id, duration=total_time)

    def _truck_process(self, truck: SimTruck) -> Generator[Any, Any, None]:
        """SimPy process: truck arrives, loads all pallets, departs.

        For each order line the forklift either loads from staging (fast)
        or from bulk storage (slow). The difference is seconds_saved.

        Args:
            truck: The SimTruck to process.
        """
        # Wait until truck arrival time
        if truck.arrival_time > self._env.now:
            yield self._env.timeout(truck.arrival_time - self._env.now)

        self._log_event("truck_arrived", dock_door=truck.appointment.dock_door)

        for order in truck.orders:
            for line in order.lines:
                yield self._env.process(self._load_line(truck, line.sku_id))

        truck.departure_time = self._env.now
        delta = truck.scheduled_departure - truck.departure_time
        if delta >= 0:
            self.metrics.early_departure_seconds += delta
        else:
            self.metrics.late_departure_seconds += abs(delta)

        self.metrics.trucks_served += 1
        self._log_event("truck_departed", dock_door=truck.appointment.dock_door, delta=delta)

    def _load_line(self, truck: SimTruck, sku_id: str) -> Generator[Any, Any, None]:
        """SimPy process: load one order line item onto the truck.

        Args:
            truck: The truck being loaded.
            sku_id: The SKU to load.
        """
        pos = self._inventory.get(sku_id)
        dock_door = truck.appointment.dock_door

        with self._forklifts.request() as req:
            yield req

            if pos is not None and pos.location.is_staging and pos.location.nearest_dock_door == dock_door:
                # Pre-staged — fast load
                base_time = self._config.loading_time_per_pallet_seconds * self._config.staging_loading_speedup
                standard_time = self._config.loading_time_per_pallet_seconds
                saved = standard_time - base_time
                self.metrics.total_seconds_saved += saved
                truck.total_loading_seconds += base_time
                truck.loads_from_staging += 1
            else:
                # Load from bulk storage — standard time
                base_time = self._config.loading_time_per_pallet_seconds
                truck.total_loading_seconds += base_time
                truck.loads_from_storage += 1

            yield self._env.timeout(base_time)

    def _order_arrival_process(self) -> Generator[Any, Any, None]:
        """SimPy process: generate new orders stochastically during the shift."""
        while self._env.now < self._config.shift_duration_seconds:
            inter_arrival = self._rng.expovariate(
                1.0 / self._config.order_inter_arrival_mean_seconds
            )
            yield self._env.timeout(inter_arrival)
            # New dynamic orders are logged but not dispatched in this stub.
            # A full implementation would add them to the closest appointment.
            self._log_event("dynamic_order_arrived", sim_time=self._env.now)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_summary_metrics(self) -> None:
        """Populate aggregate fields in self.metrics after the run."""
        total_loads = sum(t.loads_from_staging + t.loads_from_storage for t in self._trucks)
        staged_loads = sum(t.loads_from_staging for t in self._trucks)
        if total_loads > 0:
            self.metrics.pre_stage_hit_rate = staged_loads / total_loads

        if self._trucks:
            dwell_times = [
                t.departure_time - t.arrival_time
                for t in self._trucks
                if t.departure_time > 0
            ]
            if dwell_times:
                self.metrics.avg_dock_dwell_seconds = sum(dwell_times) / len(dwell_times)

    def _log_event(self, event_type: str, **kwargs: Any) -> None:
        """Record a structured simulation event.

        Args:
            event_type: Type label for the event.
            **kwargs: Additional fields to attach to the event record.
        """
        self._events.append({"sim_time": self._env.now, "event": event_type, **kwargs})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _appointment_offset(appt: CarrierAppointment, shift_duration: float) -> float:
    """Convert appointment scheduled_arrival to seconds from shift start.

    Clamps to [0, shift_duration].

    Args:
        appt: CarrierAppointment with a scheduled_arrival datetime.
        shift_duration: Total shift length in seconds.

    Returns:
        Arrival offset in seconds, clamped to shift window.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    arrival = appt.scheduled_arrival
    if arrival.tzinfo is None:
        arrival = arrival.replace(tzinfo=UTC)
    offset = (arrival - now).total_seconds()
    return max(0.0, min(offset, shift_duration))


def _departure_offset(appt: CarrierAppointment, shift_duration: float) -> float:
    """Convert appointment scheduled_departure to seconds from shift start.

    Args:
        appt: CarrierAppointment with a scheduled_departure datetime.
        shift_duration: Total shift length in seconds.

    Returns:
        Departure offset in seconds, clamped to shift window.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    departure = appt.scheduled_departure
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=UTC)
    offset = (departure - now).total_seconds()
    return max(0.0, min(offset, shift_duration))
