"""Replay-only adapter to the copied project's actual candidate pipeline.

The original scheduler receives in-memory, pallet-scoped snapshots. Only
``generate_candidates`` is called; no scheduler dispatch or live WMS method is
used. The simulator independently checks physical constraints and executes
accepted proposals only inside its private state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


POLICY_METADATA = {
    "id": "original_optimizer_phase1",
    "name": "Original optimizer · Phase 1",
    "pipeline": "PrePositionScheduler.generate_candidates",
    "source_modules": [
        "src.optimizer.scheduler", "src.scoring.value_function",
        "src.scoring.demand_predictor", "src.constraints.feasibility",
        "src.constraints.temperature", "src.constraints.capacity",
    ],
    "live_dispatch_enabled": False,
    "clock": "simulated",
    "snapshot_scope": "one physical pallet and its earliest visible matching order",
    "features": {"phase1_scoring": True, "temperature_filter": True,
                 "capacity_filter": True, "ml_prediction": False,
                 "or_assignment": False, "rl_policy": False,
                 "hazmat_filter": False},
}


def _load_optimizer():
    root = Path(__file__).resolve().parents[1] / "warehouse-preposition-optimizer"
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from src.config import ResourceConfig
        from src.constraints.capacity import CapacityConstraint
        from src.constraints.feasibility import FeasibilityEngine
        from src.constraints.temperature import TemperatureConstraint
        from src.ingestion.wms_adapter import WMSAdapter, WarehouseState
        from src.models.inventory import InventoryPosition, Location, SKU, TemperatureZone
        from src.models.orders import CarrierAppointment, OrderLine, OutboundOrder
        from src.optimizer.scheduler import PrePositionScheduler, SchedulerConfig
        from src.scoring.value_function import MovementScorer
        from src.scoring.weights import ScoringWeights
        import src.optimizer.scheduler as scheduler_module
    except ModuleNotFoundError as exc:
        raise RuntimeError("The original optimizer dependencies are unavailable. Launch with .venv-sandbox/bin/python after installing requirements-sandbox.txt; no replacement scoring fallback is used.") from exc
    if not Path(scheduler_module.__file__).resolve().is_relative_to(root.resolve()):
        raise RuntimeError("The imported optimizer resolves outside the desktop copy. Restart the sandbox with its isolated environment.")
    return {key: value for key, value in locals().items() if key in {
        "ResourceConfig", "CapacityConstraint", "FeasibilityEngine", "TemperatureConstraint",
        "WMSAdapter", "WarehouseState", "InventoryPosition", "Location", "SKU", "TemperatureZone",
        "CarrierAppointment", "OrderLine", "OutboundOrder", "PrePositionScheduler",
        "SchedulerConfig", "MovementScorer", "ScoringWeights",
    }}


class NoDispatchQueue:
    """A tripwire: any queue access means the replay boundary was violated."""

    async def push(self, *args, **kwargs):
        raise RuntimeError("Live task dispatch is disabled in the simulator sandbox")

    def __getattr__(self, name):
        raise RuntimeError(f"Task queue access '{name}' is disabled in the simulator sandbox")


class OriginalOptimizerBridge:
    def __init__(self, dataset, settings):
        self.types = _load_optimizer()
        self.settings = settings
        self.start_time = datetime.fromisoformat(dataset.get("facility", {}).get(
            "start_time", "2000-01-01T00:00:00+00:00").replace("Z", "+00:00"))
        if self.start_time.tzinfo is None:
            self.start_time = self.start_time.replace(tzinfo=timezone.utc)
        self.locations = {loc["id"]: dict(loc) for loc in dataset["locations"]}
        self.docks = {dock["id"]: dict(dock) for dock in dataset["docks"]}
        self.door_numbers = {dock_id: index + 1 for index, dock_id in enumerate(sorted(self.docks))}
        self.location_numbers = {location_id: index for index, location_id in enumerate(sorted(self.locations))}
        self._cache_time = None
        self._score_cache = {}
        self.adapter_type = _snapshot_adapter_type(self.types["WMSAdapter"])

    def _location(self, location_id, target_dock_id):
        row = self.locations[location_id]
        nearest = next((self.door_numbers[dock_id] for dock_id in sorted(self.docks)
                        if self.docks[dock_id]["staging_location_id"] == location_id), None)
        if self.docks[target_dock_id]["staging_location_id"] == location_id:
            nearest = self.door_numbers[target_dock_id]
        return self.types["Location"](
            location_id=location_id, zone=row["kind"].upper(), aisle=self.location_numbers[location_id],
            bay=0, level=0, x=row["x"], y=row["y"],
            temperature_zone=self.types["TemperatureZone"](row.get("temperature", "ambient").upper()),
            is_staging=row["kind"] == "staging", nearest_dock_door=nearest,
        )

    def _validate_candidate(self, candidate, now_seconds):
        order, pallet = candidate["order"], candidate["pallet"]
        if order["release_seconds"] > now_seconds:
            raise ValueError("An optimizer snapshot cannot include an unreleased order")
        if order["owner_id"] != pallet["owner_id"]:
            raise ValueError("An optimizer snapshot cannot mix pallet and order owners")
        if not any(line["pallet_id"] == pallet["pallet_id"] for line in order["lines"]):
            raise ValueError("An optimizer snapshot must contain matching physical-pallet demand")

    def _snapshot(self, candidate, now_seconds, resource_utilization, location_utilization):
        T = self.types
        self._validate_candidate(candidate, now_seconds)
        order, pallet = candidate["order"], candidate["pallet"]
        dock_id = order["dock_id"]
        dock = self.docks[dock_id]
        door_number = self.door_numbers[dock_id]
        source = self._location(pallet["location_id"], dock_id)
        target = self._location(candidate["destination"], dock_id)
        sku = T["SKU"](sku_id=pallet["sku_id"], description=pallet["sku_id"],
                       weight_kg=0.0, volume_m3=0.0,
                       requires_temperature_zone=T["TemperatureZone"](pallet.get("temperature", "ambient").upper()))
        position = T["InventoryPosition"](position_id=pallet["pallet_id"], sku=sku,
                                            location=source, quantity=pallet["quantity"])
        appointment = T["CarrierAppointment"](
            appointment_id=order.get("appointment_id", order["order_id"]), carrier="Historical replay",
            dock_door=door_number,
            scheduled_arrival=self.start_time + timedelta(seconds=order["scheduled_arrival_seconds"]),
            scheduled_departure=self.start_time + timedelta(seconds=order["deadline_seconds"]),
        )
        quantity = candidate.get("demand_quantity", sum(line["quantity"] for line in order["lines"]
                                                        if line["pallet_id"] == pallet["pallet_id"]))
        outbound = T["OutboundOrder"](
            order_id=order["order_id"], appointment=appointment,
            lines=[T["OrderLine"](line_id=f"{order['order_id']}:{pallet['pallet_id']}",
                                  sku_id=pallet["sku_id"], quantity=quantity, picked=False)],
            priority=order.get("priority", 1),
            cutoff_time=self.start_time + timedelta(seconds=order["deadline_seconds"]),
        )
        state = T["WarehouseState"](
            inventory_positions=[position], outbound_orders=[outbound], appointments=[appointment],
            staging_locations=[target], resource_utilization={"simulated-fleet": resource_utilization},
            location_utilization=dict(location_utilization),
        )
        return state, {door_number: (self.locations[dock["location_id"]]["x"], self.locations[dock["location_id"]]["y"])}

    async def _generate(self, candidates, now_seconds, resource_utilization, location_utilization):
        T = self.types
        result = []
        simulated_now = self.start_time + timedelta(seconds=now_seconds)
        for physical in candidates:
            self._validate_candidate(physical, now_seconds)
            order, pallet = physical["order"], physical["pallet"]
            destination = physical["destination"]
            cache_key = (pallet["pallet_id"], order["order_id"], pallet["location_id"], pallet["quantity"],
                         pallet["sku_id"], pallet.get("temperature", "ambient"),
                         destination, resource_utilization, location_utilization.get(destination, 0.0),
                         physical.get("demand_quantity"),
                         order.get("priority", 1), order["deadline_seconds"],
                         order["scheduled_arrival_seconds"])
            if cache_key not in self._score_cache:
                state, dock_coordinates = self._snapshot(physical, now_seconds, resource_utilization, location_utilization)
                resource_config = T["ResourceConfig"](
                    forklift_speed_mps=self.settings["speed_mps"],
                    handling_time_seconds=self.settings["handling_seconds"],
                    base_opportunity_seconds=60.0,
                )
                scorer = T["MovementScorer"](T["ScoringWeights"](), resource_config,
                                               dock_door_coords=dock_coordinates,
                                               clock=lambda: simulated_now)
                scheduler = T["PrePositionScheduler"](
                    scorer=scorer,
                    feasibility=T["FeasibilityEngine"]([T["TemperatureConstraint"](), T["CapacityConstraint"]()]),
                    wms=self.adapter_type(state), task_queue=NoDispatchQueue(),
                    config=T["SchedulerConfig"](
                        horizon_hours=self.settings["lookahead_minutes"] / 60,
                        max_candidates=1, min_score_threshold=self.settings["min_optimizer_score"],
                        use_or_optimization=False, use_rl_policy=False,
                    ),
                )
                original = await scheduler.generate_candidates()
                self._score_cache[cache_key] = None if not original else {
                    "score": float(original[0].score),
                    "score_components": dict(original[0].score_components),
                    "policy_source": "src.optimizer.scheduler.PrePositionScheduler.generate_candidates",
                    "optimizer_reason": original[0].reason,
                }
            scoring = self._score_cache[cache_key]
            if scoring is not None:
                scored = dict(physical)
                scored.update(scoring)
                result.append(scored)
        return sorted(result, key=lambda candidate: (-candidate["score"],
                                                     candidate["order"]["scheduled_arrival_seconds"],
                                                     candidate["order"]["order_id"],
                                                     candidate["pallet"]["pallet_id"]))

    def rank_candidates(self, physical_candidates, now_seconds, resource_utilization, location_utilization):
        if self._cache_time != now_seconds:
            self._cache_time = now_seconds
            self._score_cache.clear()
        if not physical_candidates:
            return []
        coroutine = self._generate(physical_candidates, now_seconds,
                                   resource_utilization, location_utilization)
        try:
            coroutine.send(None)
        except StopIteration as complete:
            return complete.value
        finally:
            coroutine.close()
        raise RuntimeError("The replay optimizer attempted a suspending asynchronous operation; live I/O is disabled")


def _snapshot_adapter_type(base):
    class SnapshotWMSAdapter(base):
        def __init__(self, state):
            self.state = state

        async def get_inventory_positions(self, zone=None):
            return list(self.state.inventory_positions)

        async def get_outbound_orders(self, horizon_hours=24):
            return list(self.state.outbound_orders)

        async def get_carrier_appointments(self, horizon_hours=24):
            return list(self.state.appointments)

        async def get_staging_locations(self, dock_door=None):
            return list(self.state.staging_locations)

        async def get_location_utilization(self):
            return dict(self.state.location_utilization)

        async def get_warehouse_state(self, horizon_hours=24):
            return self.state

    return SnapshotWMSAdapter
