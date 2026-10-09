"""Deterministic warehouse replay using the original optimizer for suggestions.

This module consumes plain data. It has no warehouse connection, file access,
dispatch capability. Its bridge executes only the copied optimizer's in-memory
candidate generation, constraint filters and Phase 1 scoring.
"""

from __future__ import annotations

import copy
import heapq
import math
import random
from collections import Counter
from warehouse_sandbox.optimizer_bridge import OriginalOptimizerBridge, POLICY_METADATA

MODEL_VERSION = "0.3.0"

DEFAULT_SETTINGS = {
    "speed_mps": 2.0,
    "handling_seconds": 30.0,
    "loading_seconds": 25.0,
    "lookahead_minutes": 30.0,
    "min_benefit_seconds": 5.0,
    "min_optimizer_score": 0.1,
    "adoption_percent": 100.0,
    "seed": 42,
}


def _number(value, label, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} must be finite and at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return result


def _integer(value, label, minimum=0, maximum=None):
    number = _number(value, label, minimum, maximum)
    if number != int(number):
        raise ValueError(f"{label} must be an integer")
    return int(number)


def _settings(dataset, supplied):
    settings = dict(DEFAULT_SETTINGS)
    settings["forklift_count"] = len(dataset.get("forklifts", []))
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise ValueError("settings must be an object")
        unknown = set(supplied) - set(settings)
        if unknown:
            raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
        settings.update(supplied)
    ranges = {
        "speed_mps": (0.2, 8), "handling_seconds": (0, 300),
        "loading_seconds": (0, 300), "lookahead_minutes": (1, 180),
        "min_benefit_seconds": (0, 600), "adoption_percent": (0, 100),
        "min_optimizer_score": (0, 100),
    }
    for key, bounds in ranges.items():
        settings[key] = _number(settings[key], key, *bounds)
    settings["forklift_count"] = _integer(settings["forklift_count"], "forklift_count", 1, 20)
    seed = settings["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    return settings


def _prepare(dataset):
    """Defensive checks for direct callers; the HTTP importer checks full schema."""
    if not isinstance(dataset, dict):
        raise ValueError("dataset must be an object")
    data = copy.deepcopy(dataset)
    for name in ("locations", "docks", "forklifts", "inventory", "orders"):
        if not isinstance(data.get(name), list):
            raise ValueError(f"{name} must be a list")
    if not data["forklifts"] or not data["locations"] or not data["docks"]:
        raise ValueError("At least one location, dock and forklift is required")
    if len(data["inventory"]) > 500 or len(data["orders"]) > 200:
        raise ValueError("Dataset exceeds the MVP pallet/order limit")
    data.setdefault("historical_moves", [])
    if not isinstance(data["historical_moves"], list) or len(data["historical_moves"]) > 500:
        raise ValueError("historical_moves must contain at most 500 records")

    def indexed(rows, key, label):
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"Each {label} needs a nonempty {key}")
            if row[key] in result:
                raise ValueError(f"Duplicate {label} ID: {row[key]}")
            result[row[key]] = row
        return result

    locations = indexed(data["locations"], "id", "location")
    for loc in locations.values():
        for axis in ("x", "y"):
            value = loc.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Location {loc['id']} {axis} must be finite")
        loc["capacity_pallets"] = _integer(loc.get("capacity_pallets"), "capacity_pallets", 1)
        loc.setdefault("temperature", "ambient")
    docks = indexed(data["docks"], "id", "dock")
    for dock in docks.values():
        if dock.get("location_id") not in locations or dock.get("staging_location_id") not in locations:
            raise ValueError(f"Dock {dock['id']} references an unknown location")
    indexed(data["forklifts"], "id", "forklift")
    for forklift in data["forklifts"]:
        if forklift.get("start_location_id") not in locations:
            raise ValueError("Forklift references an unknown location")
    pallets = indexed(data["inventory"], "pallet_id", "pallet")
    occupancy = Counter()
    for pallet in pallets.values():
        if pallet.get("location_id") not in locations:
            raise ValueError("Pallet references an unknown location")
        if not pallet.get("owner_id") or not pallet.get("sku_id"):
            raise ValueError("Pallet owner_id and sku_id are required")
        pallet["quantity"] = _integer(pallet.get("quantity"), "pallet quantity", 1)
        pallet.setdefault("temperature", "ambient")
        occupancy[pallet["location_id"]] += 1
    for location_id, count in occupancy.items():
        if count > locations[location_id]["capacity_pallets"]:
            raise ValueError(f"Initial inventory exceeds capacity at {location_id}")
    indexed(data["orders"], "order_id", "order")
    requested = Counter()
    appointments = {}
    line_count = 0
    for order in data["orders"]:
        if order.get("dock_id") not in docks:
            raise ValueError("Order references an unknown dock")
        for key in ("release_seconds", "arrival_seconds", "deadline_seconds"):
            order[key] = _number(order.get(key), key)
        if "scheduled_arrival_seconds" in order:
            order["scheduled_arrival_seconds"] = _number(order["scheduled_arrival_seconds"], "scheduled_arrival_seconds")
        if "priority" in order:
            order["priority"] = _integer(order["priority"], "order priority", 1, 10)
        if order["arrival_seconds"] < order["release_seconds"]:
            raise ValueError("Order arrival cannot precede release")
        if order["deadline_seconds"] < order["arrival_seconds"]:
            raise ValueError("Order deadline cannot precede arrival")
        if "observed_departure_seconds" in order:
            order["observed_departure_seconds"] = _number(order["observed_departure_seconds"], "observed_departure_seconds")
        if "appointment_id" in order:
            appointment_id = order["appointment_id"]
            if not isinstance(appointment_id, str) or not appointment_id:
                raise ValueError("appointment_id must be a nonempty string")
            metadata = (order["dock_id"], order["arrival_seconds"], order["deadline_seconds"],
                        order.get("scheduled_arrival_seconds", order["arrival_seconds"]))
            prior = appointments.setdefault(appointment_id, {"metadata": metadata, "observed": None})
            if prior["metadata"] != metadata:
                raise ValueError(f"Appointment {appointment_id} has inconsistent dock/arrival/deadline metadata")
            observed = order.get("observed_departure_seconds")
            if observed is not None:
                if prior["observed"] is not None and prior["observed"] != observed:
                    raise ValueError(f"Appointment {appointment_id} has conflicting observed departures")
                prior["observed"] = observed
        lines = order.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ValueError("Each order needs at least one line")
        line_count += len(lines)
        for line in lines:
            if not isinstance(line, dict) or line.get("pallet_id") not in pallets:
                raise ValueError("Order line references an unknown pallet")
            pallet = pallets[line["pallet_id"]]
            if pallet["owner_id"] != order.get("owner_id"):
                raise ValueError("Order cannot consume another owner's inventory")
            line["quantity"] = _integer(line.get("quantity"), "order line quantity", 1)
            requested[line["pallet_id"]] += line["quantity"]
    if line_count > 2000:
        raise ValueError("Dataset exceeds 2000 order lines")
    for pallet_id, quantity in requested.items():
        if quantity > pallets[pallet_id]["quantity"]:
            raise ValueError(f"Requested quantity exceeds inventory on pallet {pallet_id}")
    for move in data["historical_moves"]:
        if not isinstance(move, dict):
            raise ValueError("Historical move must be an object")
        move["at_seconds"] = _number(move.get("at_seconds"), "history at_seconds")
        if move.get("pallet_id") not in pallets or move.get("to_location_id") not in locations:
            raise ValueError("Historical move references an unknown pallet/location")
    return data


def _clean(value):
    """Keep the API readable while retaining enough precision for replay."""
    return round(float(value), 6)


class _Branch:
    def __init__(self, dataset, settings, proposed):
        self.data = dataset
        self.settings = settings
        self.proposed = proposed
        self.optimizer = OriginalOptimizerBridge(dataset, settings) if proposed else None
        self.locations = {loc["id"]: loc for loc in dataset["locations"]}
        self.docks = {dock["id"]: dock for dock in dataset["docks"]}
        self.pallets = {p["pallet_id"]: copy.deepcopy(p) for p in dataset["inventory"]}
        self.orders = {o["order_id"]: copy.deepcopy(o) for o in dataset["orders"]}
        self.groups = {}
        self.order_group = {}
        for order_id, order in self.orders.items():
            order.setdefault("scheduled_arrival_seconds", order["arrival_seconds"])
            group_key = ("appointment", order["appointment_id"]) if "appointment_id" in order else ("order", order_id)
            self.order_group[order_id] = group_key
            group = self.groups.setdefault(group_key, {"id": group_key[1], "order_ids": [],
                                                       "shipment_id": f"{group_key[0]}:{group_key[1]}",
                                                       "dock_id": order["dock_id"],
                                                       "arrival_seconds": order["arrival_seconds"],
                                                       "scheduled_arrival_seconds": order["scheduled_arrival_seconds"],
                                                       "deadline_seconds": order["deadline_seconds"]})
            group["order_ids"].append(order_id)
            if "appointment_id" in order:
                group["appointment_id"] = order["appointment_id"]
            if "observed_departure_seconds" in order:
                group["observed_departure_seconds"] = order["observed_departure_seconds"]
        for group in self.groups.values():
            group["order_ids"].sort()
        self.line_states = {oid: ["pending"] * len(order["lines"]) for oid, order in self.orders.items()}
        self.visible = set()
        self.arrived = set()
        self.finished = set()
        self.busy_pallets = set()
        self.busy_docks = set()
        self.reserved_staging = Counter()
        self.forklifts = []
        used_ids = {f["id"] for f in dataset["forklifts"]}
        for index in range(settings["forklift_count"]):
            original = dataset["forklifts"][index % len(dataset["forklifts"])]
            resource_id = original["id"] if index < len(dataset["forklifts"]) else f"sim-forklift-{index + 1}"
            while index >= len(dataset["forklifts"]) and resource_id in used_ids:
                resource_id += "-extra"
            used_ids.add(resource_id)
            self.forklifts.append({"id": resource_id, "location_id": original["start_location_id"], "busy": False})
        self.forklifts.sort(key=lambda f: f["id"])
        self.queue = []
        self.sequence = 0
        self.now = 0.0
        self.events = []
        self.suggestions = []
        self.considered = set()
        self.rng = random.Random(settings["seed"])
        self.trucks = []
        self.distance = 0.0
        self.busy_seconds = 0.0
        self.reposition_seconds = 0.0
        self.completed_units = 0
        self.history_applied = 0
        self.history_conflicts = 0
        self.warnings = []
        for order in sorted(dataset["orders"], key=lambda o: (o["release_seconds"], o["order_id"])):
            self.schedule(order["release_seconds"], 2, "release", order["order_id"])
        for move in sorted(dataset["historical_moves"], key=lambda m: (m["at_seconds"], m["pallet_id"], m["to_location_id"])):
            self.schedule(move["at_seconds"], 1, "history", move)

    def schedule(self, at, priority, kind, payload):
        if not math.isfinite(at):
            raise ValueError("Simulation time exceeded its finite numeric range")
        self.sequence += 1
        heapq.heappush(self.queue, (at, priority, self.sequence, kind, payload))

    def emit(self, kind, **values):
        event = {"at_seconds": _clean(self.now), "type": kind}
        for key, value in values.items():
            event[key] = _clean(value) if isinstance(value, float) else value
        self.events.append(event)

    def distance_between(self, a, b):
        left, right = self.locations[a], self.locations[b]
        distance = abs(left["x"] - right["x"]) + abs(left["y"] - right["y"])
        if not math.isfinite(distance):
            raise ValueError("Location distance exceeded its finite numeric range")
        return distance

    def occupancy(self, location_id):
        return sum(1 for pallet in self.pallets.values()
                   if pallet["quantity"] > 0 and pallet["location_id"] == location_id)

    def can_enter(self, pallet, destination):
        loc = self.locations[destination]
        return (loc.get("temperature", "ambient") == pallet.get("temperature", "ambient")
                and self.occupancy(destination) + self.reserved_staging[destination] < loc["capacity_pallets"])

    def start_load(self, forklift, order, line_index):
        line = order["lines"][line_index]
        pallet = self.pallets[line["pallet_id"]]
        origin = pallet["location_id"]
        destination = self.docks[order["dock_id"]]["location_id"]
        distance = self.distance_between(forklift["location_id"], origin) + self.distance_between(origin, destination)
        duration = distance / self.settings["speed_mps"] + self.settings["handling_seconds"] + self.settings["loading_seconds"]
        payload = {"forklift": forklift, "order_id": order["order_id"], "line_index": line_index,
                   "pallet_id": pallet["pallet_id"], "from_location_id": origin,
                   "to_location_id": destination, "duration_seconds": duration,
                   "distance_meters": distance, "quantity": line["quantity"],
                   "resource_start_location_id": forklift["location_id"],
                   "pickup_at_seconds": self.now + self.distance_between(forklift["location_id"], origin) / self.settings["speed_mps"] + self.settings["handling_seconds"],
                   "destination_arrival_seconds": self.now + distance / self.settings["speed_mps"] + self.settings["handling_seconds"]}
        if "appointment_id" in order:
            payload["appointment_id"] = order["appointment_id"]
        forklift["busy"] = True
        self.busy_pallets.add(pallet["pallet_id"])
        self.busy_docks.add(order["dock_id"])
        self.line_states[order["order_id"]][line_index] = "loading"
        self.distance += distance
        self.busy_seconds += duration
        self.emit("load_start", resource_id=forklift["id"], dock_id=order["dock_id"],
                  **{k: v for k, v in payload.items() if k not in ("forklift", "line_index")})
        self.schedule(self.now + duration, 0, "load_end", payload)

    def load_candidates(self):
        result = []
        for dock_id in sorted(self.docks):
            if dock_id in self.busy_docks:
                continue
            waiting_groups = {self.order_group[oid] for oid in self.arrived if oid not in self.finished
                              and self.orders[oid]["dock_id"] == dock_id}
            if not waiting_groups:
                continue
            group_key = min(waiting_groups, key=lambda key: (self.groups[key]["arrival_seconds"], key))
            group = self.groups[group_key]
            choice = None
            for order_id in group["order_ids"]:
                if order_id not in self.arrived or order_id in self.finished:
                    continue
                order = self.orders[order_id]
                for index, line in enumerate(order["lines"]):
                    pallet = self.pallets[line["pallet_id"]]
                    if (self.line_states[order_id][index] == "pending"
                            and pallet["pallet_id"] not in self.busy_pallets
                            and pallet["quantity"] >= line["quantity"]):
                        choice = (order, index)
                        break
                if choice:
                    break
            if choice:
                result.append(choice)
        return sorted(result, key=lambda pair: (pair[0]["arrival_seconds"], pair[0]["order_id"], pair[1]))

    def suggestion_candidates(self, forklift):
        candidates = []
        candidate_keys = set()
        next_demand = {}
        # Keep a staged partial pallet available for its earliest known shipment.
        # A later truck must not immediately undo that earlier recommendation.
        for order_id in sorted(self.visible, key=lambda oid: (self.orders[oid]["scheduled_arrival_seconds"], oid)):
            if order_id in self.finished:
                continue
            for index, line in enumerate(self.orders[order_id]["lines"]):
                if self.line_states[order_id][index] != "complete":
                    next_demand.setdefault(line["pallet_id"], order_id)
        for order_id in sorted(self.visible):
            order = self.orders[order_id]
            if order_id in self.finished or order["scheduled_arrival_seconds"] <= self.now:
                continue
            if order["scheduled_arrival_seconds"] - self.now > self.settings["lookahead_minutes"] * 60:
                continue
            dock = self.docks[order["dock_id"]]
            destination = dock["staging_location_id"]
            for index, line in enumerate(order["lines"]):
                pallet = self.pallets[line["pallet_id"]]
                key = (order_id, pallet["pallet_id"])
                origin = pallet["location_id"]
                if (key in self.considered or key in candidate_keys or self.line_states[order_id][index] != "pending"
                        or next_demand.get(pallet["pallet_id"]) != order_id
                        or pallet["pallet_id"] in self.busy_pallets
                        or pallet["quantity"] < line["quantity"] or origin == destination
                        or not self.can_enter(pallet, destination)):
                    continue
                # Both branches pay ordinary source handling and dock loading.
                # Staging saves only real loaded travel during the truck's visit.
                saved = (self.distance_between(origin, dock["location_id"])
                         - self.distance_between(destination, dock["location_id"])) / self.settings["speed_mps"]
                distance = self.distance_between(forklift["location_id"], origin) + self.distance_between(origin, destination)
                cost = distance / self.settings["speed_mps"] + self.settings["handling_seconds"]
                overlap = max(0.0, self.now + cost - order["scheduled_arrival_seconds"])
                if saved > 0 and saved >= self.settings["min_benefit_seconds"]:
                    candidate_keys.add(key)
                    demand_quantity = sum(order_line["quantity"] for line_index, order_line in enumerate(order["lines"])
                                          if order_line["pallet_id"] == pallet["pallet_id"]
                                          and self.line_states[order_id][line_index] == "pending")
                    candidates.append({"order": order, "pallet": pallet, "destination": destination,
                                       "saved": saved, "cost": cost, "overlap": overlap,
                                       "distance": distance, "demand_quantity": demand_quantity})
        utilization = sum(forklift["busy"] for forklift in self.forklifts) / len(self.forklifts)
        location_utilization = {location_id: (self.occupancy(location_id) + self.reserved_staging[location_id]) / loc["capacity_pallets"]
                                for location_id, loc in self.locations.items()}
        return self.optimizer.rank_candidates(candidates, self.now, utilization, location_utilization)

    def start_suggestion(self, forklift, candidate):
        order, pallet = candidate["order"], candidate["pallet"]
        self.considered.add((order["order_id"], pallet["pallet_id"]))
        adopted = self.rng.random() * 100 < self.settings["adoption_percent"]
        suggestion_id = f"S{len(self.suggestions) + 1:04d}"
        reason = (f"Order {order['order_id']} was released and its truck is expected in "
                  f"{order['scheduled_arrival_seconds'] - self.now:.0f}s according to its planned arrival. Move this owner's pallet closer to "
                  f"dock {order['dock_id']} using an idle forklift; loaded travel falls by "
                  f"{candidate['saved']:.1f}s. Reposition work costs {candidate['cost']:.1f}s; "
                  f"{candidate['overlap']:.1f}s extends past planned arrival. The original optimizer's "
                  f"Phase 1 value score is {candidate['score']:.4f}, using load probability, deadline urgency, "
                  f"source-to-staging cost and fleet opportunity cost. Staging capacity and temperature match.")
        if not adopted:
            reason += " The configured adoption draw declined this suggestion."
        self.suggestions.append({
            "id": suggestion_id, "at_seconds": _clean(self.now), "pallet_id": pallet["pallet_id"],
            "sku_id": pallet["sku_id"], "owner_id": pallet["owner_id"], "order_id": order["order_id"],
            "from_location_id": pallet["location_id"], "to_location_id": candidate["destination"],
            "dock_id": order["dock_id"], "estimated_load_travel_saved_seconds": _clean(candidate["saved"]),
            "move_cost_seconds": _clean(candidate["cost"]), "score": _clean(candidate["score"]),
            "score_components": {key: _clean(value) for key, value in candidate["score_components"].items()},
            "policy_source": candidate["policy_source"], "optimizer_reason": candidate["optimizer_reason"],
            "adopted": adopted, "reason": reason,
        })
        if "appointment_id" in order:
            self.suggestions[-1]["appointment_id"] = order["appointment_id"]
        if not adopted:
            return False
        payload = {"forklift": forklift, "pallet_id": pallet["pallet_id"], "order_id": order["order_id"],
                   "from_location_id": pallet["location_id"], "to_location_id": candidate["destination"],
                   "duration_seconds": candidate["cost"], "distance_meters": candidate["distance"],
                   "quantity": pallet["quantity"], "suggestion_id": suggestion_id,
                   "resource_start_location_id": forklift["location_id"],
                   "pickup_at_seconds": self.now + self.distance_between(forklift["location_id"], pallet["location_id"]) / self.settings["speed_mps"] + self.settings["handling_seconds"],
                   "destination_arrival_seconds": self.now + candidate["cost"]}
        if "appointment_id" in order:
            payload["appointment_id"] = order["appointment_id"]
        forklift["busy"] = True
        self.busy_pallets.add(pallet["pallet_id"])
        self.reserved_staging[candidate["destination"]] += 1
        self.distance += candidate["distance"]
        self.busy_seconds += candidate["cost"]
        self.reposition_seconds += candidate["cost"]
        self.emit("reposition_start", resource_id=forklift["id"],
                  **{k: v for k, v in payload.items() if k != "forklift"})
        self.schedule(self.now + candidate["cost"], 0, "reposition_end", payload)
        return True

    def dispatch(self):
        for forklift in self.forklifts:
            if forklift["busy"]:
                continue
            candidates = self.load_candidates()
            if candidates:
                self.start_load(forklift, *candidates[0])
        # Even a blocked or currently loading arrived truck takes priority over prep.
        if not self.proposed or any(oid not in self.finished for oid in self.arrived):
            return
        for forklift in self.forklifts:
            if forklift["busy"]:
                continue
            while True:
                candidates = self.suggestion_candidates(forklift)
                if not candidates or self.start_suggestion(forklift, candidates[0]):
                    break

    def historical_move(self, move):
        pallet = self.pallets[move["pallet_id"]]
        destination = move["to_location_id"]
        conflict = None
        if pallet["quantity"] <= 0:
            conflict = "pallet was already consumed"
        elif pallet["pallet_id"] in self.busy_pallets:
            conflict = "pallet was busy with simulated work"
        elif destination != pallet["location_id"] and not self.can_enter(pallet, destination):
            conflict = "destination capacity or temperature is incompatible"
        if conflict:
            self.history_conflicts += 1
            self.emit("history_conflict", pallet_id=pallet["pallet_id"], to_location_id=destination, reason=conflict)
            return
        origin = pallet["location_id"]
        pallet["location_id"] = destination
        self.history_applied += 1
        self.emit("history_move", pallet_id=pallet["pallet_id"], from_location_id=origin,
                  to_location_id=destination, quantity=pallet["quantity"], duration_seconds=0.0,
                  distance_meters=0.0, labor_unmeasured=True)

    def process(self, kind, payload):
        if kind == "release":
            order = self.orders[payload]
            self.visible.add(payload)
            self.emit("release", order_id=payload)
            self.schedule(order["arrival_seconds"], 3, "arrival", payload)
            window_start = order["scheduled_arrival_seconds"] - self.settings["lookahead_minutes"] * 60
            if self.proposed and window_start > self.now:
                self.schedule(window_start, 4, "planning_window", payload)
        elif kind == "arrival":
            self.arrived.add(payload)
            self.emit("arrival", order_id=payload, dock_id=self.orders[payload]["dock_id"])
        elif kind == "history":
            self.historical_move(payload)
        elif kind == "planning_window":
            # A known order may have been released before its lookahead window.
            # This wake-up uses only the planned arrival learned at release time.
            pass
        elif kind in ("load_end", "reposition_end"):
            forklift = payload["forklift"]
            pallet = self.pallets[payload["pallet_id"]]
            forklift["busy"] = False
            forklift["location_id"] = payload["to_location_id"]
            self.busy_pallets.remove(pallet["pallet_id"])
            self.emit(kind, resource_id=forklift["id"],
                      **{k: v for k, v in payload.items() if k not in ("forklift", "line_index")})
            if kind == "reposition_end":
                self.reserved_staging[payload["to_location_id"]] -= 1
                pallet["location_id"] = payload["to_location_id"]
            else:
                order = self.orders[payload["order_id"]]
                self.busy_docks.remove(order["dock_id"])
                pallet["quantity"] -= payload["quantity"]
                self.completed_units += payload["quantity"]
                self.line_states[order["order_id"]][payload["line_index"]] = "complete"
                if all(state == "complete" for state in self.line_states[order["order_id"]]):
                    self.finished.add(order["order_id"])
                group = self.groups[self.order_group[order["order_id"]]]
                if all(order_id in self.finished for order_id in group["order_ids"]):
                    owner_ids = sorted({self.orders[order_id]["owner_id"] for order_id in group["order_ids"]})
                    truck = {key: group[key] for key in ("shipment_id", "dock_id", "arrival_seconds", "scheduled_arrival_seconds", "deadline_seconds")}
                    truck.update(order_id=group["id"], order_ids=list(group["order_ids"]), owner_ids=owner_ids,
                                 owner_id=owner_ids[0] if len(owner_ids) == 1 else "Multiple owners")
                    if "appointment_id" in group:
                        truck["appointment_id"] = group["appointment_id"]
                    truck.update(departure_seconds=_clean(self.now),
                                 dwell_seconds=_clean(self.now - group["arrival_seconds"]),
                                 late_seconds=_clean(max(0, self.now - group["deadline_seconds"])))
                    if "observed_departure_seconds" in group:
                        truck["observed_departure_seconds"] = group["observed_departure_seconds"]
                    self.trucks.append(truck)
                    self.emit("truck_departure", order_id=group["id"], order_ids=list(group["order_ids"]), dock_id=group["dock_id"])

    def run(self):
        iterations = 0
        while self.queue and len(self.finished) < len(self.orders):
            at = self.queue[0][0]
            self.now = at
            while self.queue and self.queue[0][0] == at:
                _, _, _, kind, payload = heapq.heappop(self.queue)
                self.process(kind, payload)
                iterations += 1
                if iterations > 100000:
                    raise ValueError("Simulation stopped at its bounded event safety limit")
            self.dispatch()
        if len(self.finished) < len(self.orders):
            self.warnings.append("Simulation could not complete all orders; inspect inventory and resource conflicts.")
        dwell = sorted(t["dwell_seconds"] for t in self.trucks)
        average = sum(dwell) / len(dwell) if dwell else 0
        p95 = dwell[max(0, math.ceil(len(dwell) * 0.95) - 1)] if dwell else 0
        metrics = {
            "truck_count": len(self.trucks), "avg_dwell_seconds": _clean(average),
            "p95_dwell_seconds": _clean(p95), "late_trucks": sum(t["late_seconds"] > 0 for t in self.trucks),
            "total_lateness_seconds": _clean(sum(t["late_seconds"] for t in self.trucks)),
            "travel_meters": _clean(self.distance), "forklift_busy_seconds": _clean(self.busy_seconds),
            "makespan_seconds": _clean(max((t["departure_seconds"] for t in self.trucks), default=0)),
            "completed_units": self.completed_units,
            "remaining_units": sum(p["quantity"] for p in self.pallets.values()),
            "historical_moves_applied": self.history_applied,
            "historical_move_conflicts": self.history_conflicts,
            "historical_move_labor_unmeasured": bool(self.data["historical_moves"]),
        }
        return {"metrics": metrics, "trucks": sorted(self.trucks, key=lambda t: t["order_id"]),
                "events": self.events, "final_inventory": [self.pallets[key] for key in sorted(self.pallets)],
                "suggestions": self.suggestions}


def run_comparison(dataset: dict, settings: dict | None = None) -> dict:
    """Replay current operations and simulate deterministic suggestion adoption.

    Observed departures are used only after the branches finish, to calculate
    baseline calibration error. Suggestions never see unreleased orders.
    """
    data = _prepare(dataset)
    normalized = _settings(data, settings)
    baseline_engine = _Branch(data, normalized, proposed=False)
    proposed_engine = _Branch(data, normalized, proposed=True)
    baseline = baseline_engine.run()
    proposed = proposed_engine.run()
    base_metrics, proposed_metrics = baseline["metrics"], proposed["metrics"]
    saved = base_metrics["avg_dwell_seconds"] - proposed_metrics["avg_dwell_seconds"]
    observed = [abs(truck["departure_seconds"] - truck["observed_departure_seconds"])
                for truck in baseline["trucks"] if "observed_departure_seconds" in truck]
    warnings = [
        "Counterfactual results are model estimates, not causal proof or guaranteed savings. Validate the baseline against observed operations before relying on recommendations.",
        "Travel uses Manhattan distances and constant speed. Congestion, aisle restrictions, breaks, battery charging and equipment failures are not modeled.",
    ]
    if data["historical_moves"]:
        warnings.append("Both branches attempt recorded completed moves at their observed times without charging unknown travel or labor. Travel and forklift busy time exclude that unmeasured historical reposition work; cross-branch labor/travel deltas are incomplete.")
        warnings.append("Adopted suggestions intervene alongside recorded moves. Historical moves are never advance optimizer inputs; interventions can make an observed move infeasible or undo a previous staging choice. Inspect conflicts before interpreting the counterfactual.")
        for label, metrics in (("Historical replay", base_metrics), ("Proposed branch", proposed_metrics)):
            if metrics["historical_move_conflicts"]:
                warnings.append(f"{label} skipped {metrics['historical_move_conflicts']} move(s) because the pallet was consumed/busy or the destination was incompatible. Review replay fidelity.")
    else:
        warnings.append("No historical relocations were supplied. The baseline is a current-policy loading model rather than a replay of observed internal movements.")
    if not observed:
        warnings.append("No observed truck departures were supplied, so baseline departure calibration is unavailable.")
    missing_planned = sum("scheduled_arrival_seconds" not in order for order in data["orders"])
    if missing_planned:
        warnings.append(f"{missing_planned} order(s) have no planned arrival. Actual arrival_seconds are used as planning estimates for compatibility; this introduces hindsight if those times were not known at release. Supply scheduled_arrival_seconds to separate forecasts from observations.")
    warnings.extend(baseline_engine.warnings + proposed_engine.warnings)
    return {
        "model_version": MODEL_VERSION,
        "policy": {**copy.deepcopy(POLICY_METADATA), "min_optimizer_score": normalized["min_optimizer_score"]},
        "settings": normalized,
        "baseline_label": "Historical replay" if data["historical_moves"] else "Current policy model",
        "baseline": baseline, "proposed": proposed,
        "comparison": {
            "avg_dwell_saved_seconds": _clean(saved),
            "avg_dwell_saved_percent": _clean(saved / base_metrics["avg_dwell_seconds"] * 100) if base_metrics["avg_dwell_seconds"] else 0.0,
            "late_trucks_avoided": base_metrics["late_trucks"] - proposed_metrics["late_trucks"],
            "travel_delta_meters": _clean(proposed_metrics["travel_meters"] - base_metrics["travel_meters"]),
            "extra_reposition_seconds": _clean(proposed_engine.reposition_seconds),
        },
        "warnings": list(dict.fromkeys(warnings)),
        "assumptions": [
            "Orders, planned truck arrivals and deadlines become visible to the recommendation policy only at each order's release time. Actual arrivals drive exogenous arrival events and are not advance policy inputs; observed departures are reserved for calibration.",
            "Orders sharing an appointment_id form one truck, possibly with several owners. Without appointment_id each order is treated as a separate truck. Each dock serves arrived trucks in arrival order and permits one forklift loading visit at a time; separate docks can work concurrently.",
            "A loading visit travels from the forklift's current location to the source pallet and then to the dock, plus handling and loading time. The forklift ends at the dock.",
            "Each order line is one loading visit. Partial quantities are picked at the source; the remaining units stay on the original pallet at that location.",
            "Recommendations use only idle forklifts before known truck arrivals. Any arrived unfinished truck prevents new staging work. Existing moves finish without interruption.",
            "Pallets are exclusive while in use. Staging slots are reserved when a move starts and stay occupied until the pallet's final units are consumed or it is moved away; temperature and owner constraints apply.",
            "Suggestions are ranked by the copied original optimizer's Phase 1 V(m) value function and pass its temperature and capacity filters. Pallet-scoped snapshots preserve owner and physical identity despite the original scheduler's SKU deduplication.",
            "The original scorer uses simulated time, the visible order's priority and deadline, binary known-demand probability, actual dock coordinates and current fleet utilization. Source movement cost excludes forklift deadhead; full simulated reposition labor is separately reported.",
            "Adoption draws use a fixed seed in deterministic proposal order. Recommendations can worsen outcomes through additional work or resource contention; all differences are reported.",
            "Both branches attempt the same time-indexed historical relocations. A recorded move conflicting with simulated work, consumed inventory or capacity is skipped and reported. Unknown historical labor remains unmeasured in both branches.",
        ],
        "calibration": {"observed_trucks": len(observed),
                        "mae_departure_seconds": _clean(sum(observed) / len(observed)) if observed else None},
    }
