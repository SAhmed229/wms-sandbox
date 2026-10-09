import copy
import unittest

from warehouse_sandbox.importer import ValidationError, import_csv, normalize_settings, summarize, validate_dataset
from tests_sandbox.fixtures import csv_files, warehouse


class ImportValidationTests(unittest.TestCase):
    def assertInvalid(self, dataset):
        with self.assertRaises(ValidationError) as caught:
            validate_dataset(dataset)
        self.assertIsInstance(caught.exception.details, list)
        self.assertTrue(caught.exception.details)

    def test_normalization_does_not_mutate_customer_input(self):
        source = warehouse()
        original = copy.deepcopy(source)
        validated = validate_dataset(source)
        self.assertEqual(source, original)
        self.assertEqual(summarize(validated)["units"], 21)
        self.assertEqual(len(validated["inventory"]), 2)

    def test_shared_sku_preserves_physical_pallets_and_owners(self):
        validated = validate_dataset(warehouse())
        self.assertEqual({(p["pallet_id"], p["owner_id"]) for p in validated["inventory"]},
                         {("P1", "tenant-a"), ("P2", "tenant-b")})

    def test_cross_tenant_order_is_rejected(self):
        data = warehouse()
        data["orders"][0]["owner_id"] = "tenant-b"
        self.assertInvalid(data)

    def test_shared_appointment_requires_consistent_truck_metadata(self):
        for field in ("arrival_seconds", "deadline_seconds", "scheduled_arrival_seconds", "observed_departure_seconds"):
            with self.subTest(field=field):
                data = warehouse()
                data["orders"] = data["orders"][:2]
                for order in data["orders"]:
                    order["appointment_id"] = "TRUCK-1"
                    if field in ("scheduled_arrival_seconds", "observed_departure_seconds"):
                        order[field] = 1200
                data["orders"][1][field] += 1
                self.assertInvalid(data)

    def test_two_dock_ids_cannot_share_one_physical_door(self):
        data = warehouse()
        data["docks"].append({**data["docks"][0], "id": "same-door-alias"})
        self.assertInvalid(data)

    def test_total_partial_orders_cannot_overcommit_one_pallet(self):
        data = warehouse()
        data["orders"][2]["lines"][0]["quantity"] = 8
        self.assertInvalid(data)

    def test_invalid_references_and_duplicate_pallet_ids_are_rejected(self):
        for change in ("unknown_location", "duplicate_pallet", "unknown_order_pallet"):
            with self.subTest(change=change):
                data = warehouse()
                if change == "unknown_location":
                    data["inventory"][0]["location_id"] = "missing"
                elif change == "duplicate_pallet":
                    data["inventory"][1]["pallet_id"] = "P1"
                else:
                    data["orders"][0]["lines"][0]["pallet_id"] = "missing"
                self.assertInvalid(data)

    def test_nonfinite_numbers_and_fractional_units_are_rejected(self):
        for value in (float("nan"), float("inf"), -1, 1.5):
            with self.subTest(value=value):
                data = warehouse()
                data["inventory"][0]["quantity"] = value
                self.assertInvalid(data)
        data = warehouse()
        data["locations"][0]["x"] = float("inf")
        self.assertInvalid(data)

    def test_timezone_and_timeline_validation(self):
        data = warehouse()
        data["facility"]["start_time"] = "2026-10-08T08:00:00"
        self.assertInvalid(data)
        data = warehouse()
        data["orders"][0]["release_seconds"] = -1
        self.assertInvalid(data)
        data = warehouse()
        data["orders"][0]["deadline_seconds"] = data["orders"][0]["arrival_seconds"] - 1
        self.assertInvalid(data)

    def test_temperature_incompatible_inventory_is_rejected(self):
        data = warehouse()
        data["inventory"][0]["temperature"] = "frozen"
        self.assertInvalid(data)

    def test_malformed_enum_types_produce_validation_errors(self):
        for table, field, value in (("locations", "kind", ["storage"]),
                                    ("locations", "temperature", {"name": "ambient"}),
                                    ("inventory", "temperature", ["ambient"])):
            with self.subTest(table=table, field=field):
                data = warehouse()
                data[table][0][field] = value
                self.assertInvalid(data)

    def test_csv_groups_lines_without_merging_same_sku_pallets(self):
        data = import_csv(csv_files(), facility_name="CSV facility", start_time="2026-10-08T08:00:00-04:00")
        self.assertEqual(data["facility"]["name"], "CSV facility")
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(len(data["orders"][0]["lines"]), 2)
        self.assertEqual(summarize(data)["units"], 12)

    def test_csv_repeated_order_metadata_must_agree(self):
        files = csv_files()
        files["orders.csv"] = files["orders.csv"].replace("O1,A,0,400,700,D1,P2", "O1,A,0,450,700,D1,P2")
        with self.assertRaises(ValidationError):
            import_csv(files, start_time="2026-10-08T08:00:00-04:00")

    def test_initial_physical_capacity_cannot_be_exceeded(self):
        data = warehouse()
        data["inventory"][0]["location_id"] = "stage"
        data["inventory"][1]["location_id"] = "stage"
        self.assertInvalid(data)

    def test_settings_reject_invalid_physical_assumptions_and_unknown_controls(self):
        data = validate_dataset(warehouse())
        for settings in ({"speed_mps": 0}, {"forklift_count": 0}, {"forklift_count": 1.5},
                         {"handling_seconds": -1}, {"adoption_percent": 101},
                         {"seed": True}, {"dispatch_to_wms": True}):
            with self.subTest(settings=settings):
                with self.assertRaises(ValidationError):
                    normalize_settings(settings, data)


if __name__ == "__main__":
    unittest.main()
