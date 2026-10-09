"""Normalize authorized historical exports into the sandbox's common model."""

from __future__ import annotations

import csv
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

LIMITS = {"request_bytes": 5 * 1024 * 1024, "pallets": 500, "orders": 200,
          "order_lines": 2000, "historical_moves": 500, "locations": 600,
          "horizon_seconds": 172800}
DEFAULT_SETTINGS = {"forklift_count": 3, "speed_mps": 2.0, "handling_seconds": 30.0,
                    "loading_seconds": 25.0, "lookahead_minutes": 30.0,
                    "min_benefit_seconds": 5.0, "min_optimizer_score": 0.1, "adoption_percent": 100.0, "seed": 42}
CSV_TABLES = {
    "locations.csv": ["id", "x", "y", "kind", "capacity_pallets", "temperature"],
    "docks.csv": ["id", "location_id", "staging_location_id"],
    "forklifts.csv": ["id", "start_location_id"],
    "inventory.csv": ["pallet_id", "sku_id", "owner_id", "location_id", "quantity", "temperature"],
    "orders.csv": ["order_id", "owner_id", "release_seconds", "arrival_seconds", "deadline_seconds",
                   "dock_id", "pallet_id", "quantity", "observed_departure_seconds", "scheduled_arrival_seconds", "appointment_id", "priority"],
    "historical_moves.csv": ["at_seconds", "pallet_id", "to_location_id"],
}
TEMPERATURES = {"ambient", "chilled", "frozen"}


class ValidationError(ValueError):
    def __init__(self, details: list[str]):
        self.details = details
        super().__init__("Dataset validation failed" if len(details) != 1 else details[0])


def validate_dataset(data: Any) -> dict:
    """Validate references, timeline and inventory conservation without mutating input."""
    if not isinstance(data, dict):
        raise ValidationError(["Dataset must be a JSON object."])
    errors: list[str] = []

    def number(value, label, low=0, high=LIMITS["horizon_seconds"], integer=False):
        try:
            if isinstance(value, bool) or value is None:
                raise ValueError
            val = float(value)
            if not math.isfinite(val) or not low <= val <= high or (integer and not val.is_integer()):
                raise ValueError
            return int(val) if integer else val
        except (ValueError, TypeError, OverflowError):
            errors.append(f"{label} must be {'an integer' if integer else 'a finite number'} from {low} to {high}.")
            return int(low) if integer else float(low)

    def ident(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            errors.append(f"{label} must be a nonempty string of at most 160 characters.")
            return ""
        return value.strip()

    def rows(name, limit, required=True):
        value = data.get(name, [])
        if not isinstance(value, list) or (required and not value):
            errors.append(f"{name} must be {'a nonempty' if required else 'an'} array.")
            return []
        if len(value) > limit:
            errors.append(f"{name} exceeds the MVP limit of {limit} records.")
            return []
        if any(not isinstance(row, dict) for row in value):
            errors.append(f"Every {name} record must be an object.")
            return []
        return value

    def unique(items, field, label):
        for key, count in Counter(i[field] for i in items).items():
            if count > 1:
                errors.append(f"Duplicate {label}: {key}.")

    if data.get("schema_version", "1.0") != "1.0":
        errors.append("Only schema_version 1.0 is supported.")
    facility = data.get("facility", {})
    if not isinstance(facility, dict):
        facility = {}
        errors.append("facility must be an object.")
    name = ident(facility.get("name"), "facility.name")
    start_time = facility.get("start_time")
    try:
        parsed = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        start_time = parsed.isoformat()
    except (AttributeError, TypeError, ValueError):
        errors.append("facility.start_time must be an ISO datetime with a timezone offset.")
        start_time = "2026-09-14T06:00:00-04:00"

    locations = []
    for i, row in enumerate(rows("locations", LIMITS["locations"])):
        tag = f"locations[{i}]"
        kind = row.get("kind")
        if not isinstance(kind, str) or kind not in {"storage", "staging", "dock"}:
            errors.append(f"{tag}.kind must be storage, staging or dock.")
        temp = row.get("temperature", "ambient")
        if temp is None or temp == "":
            temp = "ambient"
        if not isinstance(temp, str) or temp not in TEMPERATURES:
            errors.append(f"{tag}.temperature must be ambient, chilled or frozen.")
        locations.append({"id": ident(row.get("id"), tag + ".id"),
                          "x": number(row.get("x"), tag + ".x", -10000, 10000),
                          "y": number(row.get("y"), tag + ".y", -10000, 10000),
                          "kind": kind, "temperature": temp,
                          "capacity_pallets": number(row.get("capacity_pallets"), tag + ".capacity_pallets", 1, 500, True)})
    unique(locations, "id", "location ID")
    locs = {row["id"]: row for row in locations}

    docks = []
    for i, row in enumerate(rows("docks", 50)):
        tag = f"docks[{i}]"
        dock = {key: ident(row.get(key), tag + "." + key) for key in CSV_TABLES["docks.csv"]}
        for field, kind in [("location_id", "dock"), ("staging_location_id", "staging")]:
            if dock[field] not in locs or locs[dock[field]]["kind"] != kind:
                errors.append(f"{tag}.{field} must refer to a {kind} location.")
        docks.append(dock)
    unique(docks, "id", "dock ID")
    unique(docks, "location_id", "physical dock location")
    dock_ids = {row["id"] for row in docks}

    forklifts = []
    for i, row in enumerate(rows("forklifts", 20)):
        tag = f"forklifts[{i}]"
        fork = {key: ident(row.get(key), tag + "." + key) for key in CSV_TABLES["forklifts.csv"]}
        if fork["start_location_id"] not in locs:
            errors.append(f"{tag}.start_location_id is unknown.")
        forklifts.append(fork)
    unique(forklifts, "id", "forklift ID")

    inventory = []
    for i, row in enumerate(rows("inventory", LIMITS["pallets"])):
        tag = f"inventory[{i}]"
        pallet = {key: ident(row.get(key), tag + "." + key)
                  for key in ["pallet_id", "sku_id", "owner_id", "location_id"]}
        pallet["quantity"] = number(row.get("quantity"), tag + ".quantity", 1, 1000000, True)
        pallet["temperature"] = row.get("temperature", "ambient")
        if pallet["temperature"] is None or pallet["temperature"] == "":
            pallet["temperature"] = "ambient"
        if not isinstance(pallet["temperature"], str) or pallet["temperature"] not in TEMPERATURES:
            errors.append(f"{tag}.temperature must be ambient, chilled or frozen.")
        if pallet["location_id"] not in locs:
            errors.append(f"{tag}.location_id is unknown.")
        elif locs[pallet["location_id"]]["kind"] == "dock":
            errors.append(f"{tag} must begin at storage or staging, not a dock.")
        elif locs[pallet["location_id"]]["temperature"] != pallet["temperature"]:
            errors.append(f"{tag} temperature does not match its initial location.")
        inventory.append(pallet)
    unique(inventory, "pallet_id", "pallet ID")
    pallets = {row["pallet_id"]: row for row in inventory}
    occupancy = Counter(row["location_id"] for row in inventory)
    for location_id, count in occupancy.items():
        if location_id in locs and count > locs[location_id]["capacity_pallets"]:
            errors.append(f"Initial inventory exceeds capacity at {location_id}.")

    orders = []
    demand: dict[str, int] = defaultdict(int)
    line_count = 0
    for i, row in enumerate(rows("orders", LIMITS["orders"])):
        tag = f"orders[{i}]"
        order = {key: ident(row.get(key), tag + "." + key) for key in ["order_id", "owner_id", "dock_id"]}
        priority = row.get("priority", 1)
        if priority is None or priority == "":
            priority = 1
        order["priority"] = number(priority, tag + ".priority", 1, 10, True)
        if order["dock_id"] not in dock_ids:
            errors.append(f"{tag}.dock_id is unknown.")
        for field in ["release_seconds", "arrival_seconds", "deadline_seconds"]:
            order[field] = number(row.get(field), tag + "." + field)
        if row.get("scheduled_arrival_seconds") not in (None, ""):
            order["scheduled_arrival_seconds"] = number(row["scheduled_arrival_seconds"], tag + ".scheduled_arrival_seconds")
        if row.get("appointment_id") not in (None, ""):
            order["appointment_id"] = ident(row["appointment_id"], tag + ".appointment_id")
        if order["release_seconds"] > order["arrival_seconds"]:
            errors.append(f"{tag} release must be at or before truck arrival.")
        if order["deadline_seconds"] < order["arrival_seconds"]:
            errors.append(f"{tag} deadline must be at or after truck arrival.")
        observed = row.get("observed_departure_seconds")
        if observed not in (None, ""):
            order["observed_departure_seconds"] = number(observed, tag + ".observed_departure_seconds")
            if order["observed_departure_seconds"] < order["arrival_seconds"]:
                errors.append(f"{tag} observed departure precedes arrival.")
        lines = row.get("lines")
        if not isinstance(lines, list) or not lines or any(not isinstance(line, dict) for line in lines):
            errors.append(f"{tag}.lines must be a nonempty array of objects.")
            lines = []
        if len(lines) > LIMITS["order_lines"]:
            errors.append(f"{tag}.lines exceeds the MVP limit.")
            lines = []
        order["lines"] = []
        for j, line in enumerate(lines):
            ltag = f"{tag}.lines[{j}]"
            pid = ident(line.get("pallet_id"), ltag + ".pallet_id")
            qty = number(line.get("quantity"), ltag + ".quantity", 1, 1000000, True)
            if pid not in pallets:
                errors.append(f"{ltag}.pallet_id is unknown.")
            elif pallets[pid]["owner_id"] != order["owner_id"]:
                errors.append(f"{ltag} pallet owner does not match the order owner.")
            demand[pid] += qty
            order["lines"].append({"pallet_id": pid, "quantity": qty})
        line_count += len(lines)
        orders.append(order)
    unique(orders, "order_id", "order ID")
    appointments = {}
    for order in orders:
        if "appointment_id" not in order:
            continue
        aid = order["appointment_id"]
        metadata = (order["dock_id"], order["arrival_seconds"], order["deadline_seconds"],
                    order.get("scheduled_arrival_seconds", order["arrival_seconds"]))
        group = appointments.setdefault(aid, {"metadata": metadata, "observed": set()})
        if group["metadata"] != metadata:
            errors.append(f"Orders in appointment {aid} must share dock, actual/planned arrival and deadline.")
        if "observed_departure_seconds" in order:
            group["observed"].add(order["observed_departure_seconds"])
        if len(group["observed"]) > 1:
            errors.append(f"Orders in appointment {aid} must have the same observed truck departure.")
    if line_count > LIMITS["order_lines"]:
        errors.append(f"Total order lines exceeds the MVP limit of {LIMITS['order_lines']}.")
    for pid, quantity in demand.items():
        if pid in pallets and quantity > pallets[pid]["quantity"]:
            errors.append(f"Orders request {quantity} units of {pid}, but only {pallets[pid]['quantity']} exist.")

    moves = []
    for i, row in enumerate(rows("historical_moves", LIMITS["historical_moves"], False)):
        tag = f"historical_moves[{i}]"
        move = {"at_seconds": number(row.get("at_seconds"), tag + ".at_seconds"),
                "pallet_id": ident(row.get("pallet_id"), tag + ".pallet_id"),
                "to_location_id": ident(row.get("to_location_id"), tag + ".to_location_id")}
        if move["pallet_id"] not in pallets:
            errors.append(f"{tag}.pallet_id is unknown.")
        if move["to_location_id"] not in locs or locs[move["to_location_id"]]["kind"] == "dock":
            errors.append(f"{tag}.to_location_id must be storage or staging.")
        elif move["pallet_id"] in pallets and pallets[move["pallet_id"]]["temperature"] != locs[move["to_location_id"]]["temperature"]:
            errors.append(f"{tag} has an incompatible destination temperature.")
        moves.append(move)
    if errors:
        raise ValidationError(errors[:80])
    return {"schema_version": "1.0", "facility": {"name": name, "start_time": start_time},
            "locations": locations, "docks": docks, "forklifts": forklifts,
            "inventory": inventory, "orders": orders,
            "historical_moves": sorted(moves, key=lambda r: r["at_seconds"])}


def normalize_settings(settings: Any, dataset: dict) -> dict:
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValidationError(["settings must be an object."])
    defaults = {**DEFAULT_SETTINGS, "forklift_count": len(dataset["forklifts"])}
    ranges = {"forklift_count": (1, 20, True), "speed_mps": (0.2, 8, False),
              "handling_seconds": (0, 300, False), "loading_seconds": (0, 300, False),
              "lookahead_minutes": (1, 180, False), "min_benefit_seconds": (0, 600, False),
              "min_optimizer_score": (0, 100, False),
              "adoption_percent": (0, 100, False), "seed": (0, 2147483647, True)}
    result, errors = {}, []
    for key, (low, high, integer) in ranges.items():
        value = settings.get(key, defaults[key])
        try:
            if isinstance(value, bool):
                raise ValueError
            value = float(value)
            if not math.isfinite(value) or not low <= value <= high or (integer and not value.is_integer()):
                raise ValueError
            result[key] = int(value) if integer else value
        except (ValueError, TypeError, OverflowError):
            errors.append(f"settings.{key} must be {'an integer' if integer else 'a finite number'} from {low} to {high}.")
    unknown = set(settings) - set(ranges)
    if unknown:
        errors.append("Unknown settings: " + ", ".join(sorted(unknown)))
    if errors:
        raise ValidationError(errors)
    return result


def import_csv(files: Any, facility_name="Imported warehouse", start_time="2026-09-14T06:00:00-04:00") -> dict:
    """Read CSV text in memory; never interpret uploads as filesystem paths."""
    if not isinstance(files, dict):
        raise ValidationError(["files must map CSV filenames to CSV text."])
    normalized = {}
    errors = []
    for filename, text in files.items():
        if filename not in CSV_TABLES:
            errors.append(f"Unsupported CSV file {filename}; use the template filenames.")
            continue
        if not isinstance(text, str):
            errors.append(f"{filename} must contain text.")
            continue
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                errors.append(f"{filename} contains duplicate column names.")
            optional = {"temperature", "observed_departure_seconds", "scheduled_arrival_seconds", "appointment_id", "priority"}
            missing = set(CSV_TABLES[filename]) - set(headers) - optional
            if missing:
                errors.append(f"{filename} is missing columns: {', '.join(sorted(missing))}.")
            records = []
            cap = LIMITS["order_lines"] if filename == "orders.csv" else 600
            for index, row in enumerate(reader):
                if index >= cap:
                    errors.append(f"{filename} exceeds the MVP record limit.")
                    break
                if None in row or any(value is None for value in row.values()):
                    errors.append(f"{filename} row {index + 2} has an incorrect number of columns.")
                    continue
                if any((value or "").strip() for value in row.values()):
                    records.append({key.strip(): value.strip() for key, value in row.items()})
            normalized[filename] = records
        except csv.Error as exc:
            errors.append(f"Invalid CSV in {filename}: {exc}.")
    required = set(CSV_TABLES) - {"historical_moves.csv"}
    missing = required - set(normalized)
    if missing:
        errors.append("Missing CSV tables: " + ", ".join(sorted(missing)))
    if errors:
        raise ValidationError(errors)
    grouped = {}
    metadata_fields = ["order_id", "owner_id", "release_seconds", "arrival_seconds", "deadline_seconds", "dock_id", "observed_departure_seconds", "scheduled_arrival_seconds", "appointment_id", "priority"]
    for row in normalized["orders.csv"]:
        oid = row["order_id"]
        meta = {key: row.get(key, "") for key in metadata_fields}
        if oid not in grouped:
            grouped[oid] = {**meta, "lines": []}
        elif any(grouped[oid][key] != value for key, value in meta.items()):
            errors.append(f"orders.csv repeats order {oid} with inconsistent metadata.")
        grouped[oid]["lines"].append({"pallet_id": row["pallet_id"], "quantity": row["quantity"]})
    if errors:
        raise ValidationError(errors)
    return validate_dataset({"schema_version": "1.0", "facility": {"name": facility_name, "start_time": start_time},
                             "locations": normalized["locations.csv"], "docks": normalized["docks.csv"],
                             "forklifts": normalized["forklifts.csv"], "inventory": normalized["inventory.csv"],
                             "orders": list(grouped.values()), "historical_moves": normalized.get("historical_moves.csv", [])})


def summarize(dataset: dict) -> dict:
    return {"facility_name": dataset["facility"]["name"], "pallets": len(dataset["inventory"]),
            "orders": len(dataset["orders"]), "locations": len(dataset["locations"]),
            "docks": len(dataset["docks"]), "forklifts": len(dataset["forklifts"]),
            "history_moves": len(dataset.get("historical_moves", [])),
            "units": sum(p["quantity"] for p in dataset["inventory"])}


def normalize_json_import(data: Any) -> tuple[dict, dict | None]:
    """Accept a raw dataset or a full exported run, including UTF-8 BOM text."""
    if isinstance(data, str):
        data = json.loads(data.lstrip().lstrip("\ufeff"))
    saved_settings = None
    if isinstance(data, dict) and "dataset" in data:
        wrapper = data
        data = wrapper["dataset"]
        if "settings" in wrapper:
            saved_settings = wrapper["settings"]
    dataset = validate_dataset(data)
    return dataset, normalize_settings(saved_settings, dataset) if saved_settings is not None else None
