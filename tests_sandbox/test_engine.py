import copy
import socket
import unittest
from collections import defaultdict
from unittest.mock import patch

from warehouse_sandbox.engine import run_comparison
from warehouse_sandbox.importer import validate_dataset
from tests_sandbox.fixtures import warehouse


def validated_warehouse():
    return validate_dataset(warehouse())


def intervals(branch, resource_key):
    """Read physical occupation directly from task start events."""
    grouped = defaultdict(list)
    for event in branch["events"]:
        if event["type"] not in ("load_start", "reposition_start"):
            continue
        resource = resource_key(event)
        if resource is not None:
            start = event["at_seconds"]
            grouped[resource].append((start, start + event["duration_seconds"], event))
    return grouped


class EngineInvariantTests(unittest.TestCase):
    def assertExclusion(self, grouped):
        for resource, ranges in grouped.items():
            previous_end = -1
            for start, end, event in sorted(ranges, key=lambda row: row[0]):
                self.assertGreaterEqual(start + 1e-7, previous_end, (resource, event))
                self.assertGreaterEqual(end, start)
                previous_end = end

    def test_input_and_branch_states_are_independent(self):
        data = validated_warehouse()
        original = copy.deepcopy(data)
        result = run_comparison(data)
        self.assertEqual(data, original)
        baseline = result["baseline"]["final_inventory"]
        proposed = result["proposed"]["final_inventory"]
        self.assertIsNot(baseline, proposed)
        proposed[0]["quantity"] = 9999
        self.assertNotEqual(baseline[0]["quantity"], 9999)
        self.assertEqual(data, original)

    def test_same_seed_is_exactly_reproducible(self):
        data = validated_warehouse()
        settings = {"seed": 123, "adoption_percent": 50}
        self.assertEqual(run_comparison(data, settings), run_comparison(data, settings))

    def test_appointment_and_order_namespaces_preserve_distinct_shipments(self):
        data = warehouse()
        data["orders"][0]["appointment_id"] = data["orders"][1]["order_id"]
        result = run_comparison(validate_dataset(data))
        for branch in (result["baseline"], result["proposed"]):
            keys = [truck["shipment_id"] for truck in branch["trucks"]]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(len(keys), len(data["orders"]))

    def test_partial_orders_conserve_units_and_physical_identity(self):
        data = validated_warehouse()
        result = run_comparison(data)
        for name in ("baseline", "proposed"):
            with self.subTest(branch=name):
                branch = result[name]
                inventory = {p["pallet_id"]: p for p in branch["final_inventory"]}
                self.assertEqual(set(inventory), {"P1", "P2"})
                self.assertEqual(inventory["P1"]["quantity"], 3)
                self.assertEqual(inventory["P2"]["quantity"], 0)
                self.assertEqual(inventory["P1"]["owner_id"], "tenant-a")
                self.assertEqual(inventory["P2"]["owner_id"], "tenant-b")
                self.assertEqual(branch["metrics"]["completed_units"], 18)
                self.assertEqual(branch["metrics"]["remaining_units"], 3)
                consumed = sum(e.get("quantity", 0) for e in branch["events"] if e["type"] == "load_end")
                self.assertEqual(consumed, 18)
                self.assertEqual(consumed + sum(p["quantity"] for p in inventory.values()), 21)

    def test_forklifts_pallets_and_docks_are_mutually_exclusive(self):
        data = validated_warehouse()
        data["forklifts"].append({"id": "F2", "start_location_id": "store-b"})
        result = run_comparison(data)
        order_docks = {o["order_id"]: o["dock_id"] for o in data["orders"]}
        for branch in (result["baseline"], result["proposed"]):
            self.assertExclusion(intervals(branch, lambda e: e["resource_id"]))
            self.assertExclusion(intervals(branch, lambda e: e["pallet_id"]))
            self.assertExclusion(intervals(branch, lambda e: order_docks[e["order_id"]] if e["type"] == "load_start" else None))

    def test_staging_reservations_and_partial_pallets_obey_capacity(self):
        data = validated_warehouse()
        data["forklifts"].append({"id": "F2", "start_location_id": "store-b"})
        branch = run_comparison(data)["proposed"]
        self.assertTrue(branch["suggestions"], "fixture should exercise staging proposals")
        reserved = set()
        quantity = {p["pallet_id"]: p["quantity"] for p in data["inventory"]}
        for event in branch["events"]:
            pallet = event.get("pallet_id")
            if event["type"] == "reposition_start" and event["to_location_id"] == "stage":
                reserved.add(pallet)
            elif event["type"] == "load_end":
                quantity[pallet] -= event["quantity"]
                if quantity[pallet] == 0:
                    reserved.discard(pallet)
            self.assertLessEqual(len(reserved), 1, event)

    def test_policy_does_not_see_future_order_releases(self):
        first = validated_warehouse()
        second = copy.deepcopy(first)
        second["orders"][2]["arrival_seconds"] = 2500
        second["orders"][2]["deadline_seconds"] = 3000
        a = run_comparison(first)["proposed"]
        b = run_comparison(second)["proposed"]
        before_release = lambda branch: [e for e in branch["events"] if e["at_seconds"] < 1500]
        self.assertEqual(before_release(a), before_release(b))
        self.assertFalse(any(s["order_id"] == "O3" and s["at_seconds"] < 1500 for s in a["suggestions"]))

    def test_observed_departures_are_calibration_only(self):
        data = validated_warehouse()
        observed = copy.deepcopy(data)
        for order in observed["orders"]:
            order["observed_departure_seconds"] = order["arrival_seconds"] + 9000
        a = run_comparison(data)
        b = run_comparison(observed)
        for branch in ("baseline", "proposed"):
            self.assertEqual(a[branch]["events"], b[branch]["events"])
            self.assertEqual(a[branch]["metrics"], b[branch]["metrics"])
            self.assertEqual(a[branch]["suggestions"], b[branch]["suggestions"])
        self.assertEqual(b["calibration"]["observed_trucks"], 3)
        self.assertGreater(b["calibration"]["mae_departure_seconds"], 0)

    def test_zero_adoption_matches_current_policy_operations(self):
        result = run_comparison(validated_warehouse(), {"adoption_percent": 0})
        self.assertTrue(result["proposed"]["suggestions"])
        self.assertFalse(any(s["adopted"] for s in result["proposed"]["suggestions"]))
        self.assertEqual(result["baseline"]["metrics"], result["proposed"]["metrics"])
        self.assertEqual(result["baseline"]["final_inventory"], result["proposed"]["final_inventory"])
        self.assertEqual(result["comparison"]["avg_dwell_saved_seconds"], 0)

    def test_zero_adoption_replays_the_same_historical_moves(self):
        data = warehouse()
        data["historical_moves"] = [{"at_seconds": 100, "pallet_id": "P1", "to_location_id": "stage"}]
        result = run_comparison(validate_dataset(data), {"adoption_percent": 0})
        self.assertEqual(result["baseline"]["metrics"], result["proposed"]["metrics"])
        self.assertEqual(result["baseline"]["final_inventory"], result["proposed"]["final_inventory"])
        self.assertEqual(result["baseline"]["trucks"], result["proposed"]["trucks"])
        self.assertEqual(result["comparison"]["avg_dwell_saved_seconds"], 0)
        self.assertTrue(result["proposed"]["metrics"]["historical_move_labor_unmeasured"])

    def test_planned_arrival_drives_prearrival_policy_without_actual_arrival_hindsight(self):
        data = warehouse()
        data["orders"] = [data["orders"][0]]
        data["orders"][0].update(scheduled_arrival_seconds=2000, arrival_seconds=1800, deadline_seconds=3000)
        a = run_comparison(validate_dataset(data), {"lookahead_minutes": 10})
        later = copy.deepcopy(data)
        later["orders"][0]["arrival_seconds"] = 2400
        b = run_comparison(validate_dataset(later), {"lookahead_minutes": 10})
        before_first_actual_arrival = lambda branch: [e for e in branch["events"] if e["at_seconds"] < 1800]
        self.assertEqual(before_first_actual_arrival(a["proposed"]), before_first_actual_arrival(b["proposed"]))
        self.assertEqual(a["proposed"]["suggestions"], b["proposed"]["suggestions"])
        self.assertTrue(a["proposed"]["suggestions"])
        self.assertEqual(a["proposed"]["suggestions"][0]["at_seconds"], 1400)
        self.assertEqual(a["proposed"]["trucks"][0]["arrival_seconds"], 1800)
        self.assertEqual(b["proposed"]["trucks"][0]["arrival_seconds"], 2400)
        self.assertEqual(b["proposed"]["trucks"][0]["departure_seconds"] - a["proposed"]["trucks"][0]["departure_seconds"], 600)

    def test_shared_appointment_counts_one_truck_across_customer_orders(self):
        data = warehouse()
        data["orders"] = data["orders"][:2]
        for order in data["orders"]:
            order["appointment_id"] = "TRUCK-1"
        result = run_comparison(validate_dataset(data))
        for name in ("baseline", "proposed"):
            with self.subTest(branch=name):
                branch = result[name]
                self.assertEqual(branch["metrics"]["truck_count"], 1)
                self.assertEqual(len(branch["trucks"]), 1)
                truck = branch["trucks"][0]
                self.assertEqual(truck["appointment_id"], "TRUCK-1")
                self.assertEqual(set(truck["order_ids"]), {"O1", "O2"})
                self.assertEqual(set(truck["owner_ids"]), {"tenant-a", "tenant-b"})
                self.assertEqual(branch["metrics"]["completed_units"], 14)
                self.assertEqual(sum(e["type"] == "truck_departure" for e in branch["events"]), 1)

    def test_full_adoption_can_reduce_dwell_with_available_prearrival_time(self):
        data = warehouse()
        data["orders"] = [data["orders"][0]]
        data = validate_dataset(data)
        result = run_comparison(data, {"adoption_percent": 100})
        self.assertTrue(any(s["adopted"] for s in result["proposed"]["suggestions"]))
        self.assertGreater(result["comparison"]["avg_dwell_saved_seconds"], 0)
        self.assertEqual(result["baseline"]["metrics"]["completed_units"], 5)
        self.assertEqual(result["proposed"]["metrics"]["completed_units"], 5)

    def test_historical_conflicts_are_visible_and_labor_is_unmeasured(self):
        data = warehouse()
        data["orders"] = [data["orders"][1], data["orders"][0]]
        data["orders"][1]["arrival_seconds"] = 1400
        data["orders"][1]["deadline_seconds"] = 2000
        data["historical_moves"] = [
            {"at_seconds": 100, "pallet_id": "P1", "to_location_id": "stage"},
            {"at_seconds": 1001, "pallet_id": "P2", "to_location_id": "store-a"},
            {"at_seconds": 1200, "pallet_id": "P2", "to_location_id": "store-a"},
        ]
        result = run_comparison(validate_dataset(data))
        metrics = result["baseline"]["metrics"]
        self.assertEqual(result["baseline_label"], "Historical replay")
        self.assertEqual(metrics["historical_moves_applied"], 1)
        self.assertEqual(metrics["historical_move_conflicts"], 2)
        self.assertTrue(metrics["historical_move_labor_unmeasured"])
        self.assertTrue(any("histor" in warning.lower() for warning in result["warnings"]))

    def test_load_time_uses_travel_and_handling_without_staging_discount(self):
        data = warehouse()
        data["orders"] = [data["orders"][0]]
        data["orders"][0].update(release_seconds=0, arrival_seconds=0, deadline_seconds=1000)
        data["forklifts"][0]["start_location_id"] = "dock"
        result = run_comparison(validate_dataset(data), {
            "speed_mps": 2, "handling_seconds": 30, "loading_seconds": 25,
        })
        # Dock -> storage is 120 m, loaded return is 120 m: 240/2 + 30 + 25.
        self.assertAlmostEqual(result["baseline"]["trucks"][0]["departure_seconds"], 175)
        self.assertAlmostEqual(result["baseline"]["metrics"]["travel_meters"], 240)
        self.assertEqual(result["proposed"]["suggestions"], [])

    def test_engine_operates_without_network_access(self):
        # An asyncio event loop may create a local self-pipe/socket pair. Guard
        # connection attempts, which are the actual external-access boundary.
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network access is forbidden")), \
             patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network access is forbidden")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("network access is forbidden")):
            result = run_comparison(validated_warehouse())
        self.assertEqual(result["baseline"]["metrics"]["truck_count"], 3)


if __name__ == "__main__":
    unittest.main()
