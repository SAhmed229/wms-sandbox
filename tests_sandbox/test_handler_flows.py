"""Full request/response flows through the HTTP handler without a socket bind.

The separate test_server suite also checks the live loopback listener. These
tests keep import/export lifecycle checks runnable in restricted environments.
"""

import csv
import http.client
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from warehouse_sandbox.server import SandboxHandler
from tests_sandbox.fixtures import csv_files, warehouse


class MemoryConnection:
    def __init__(self, request):
        self.incoming = io.BytesIO(request)
        self.outgoing = bytearray()

    def makefile(self, mode, *args):
        return self.incoming

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, timeout):
        pass


class ResponseConnection:
    def __init__(self, response):
        self.response = response

    def makefile(self, mode, *args):
        return io.BytesIO(self.response)


class HandlerUserFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="warehouse-handler-test-")
        self.addCleanup(self.directory.cleanup)
        self.server = SimpleNamespace(server_address=("127.0.0.1", 8876), data_dir=Path(self.directory.name))

    def request(self, method, path, body=None):
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1:8876", "Connection: close"]
        if body is not None:
            headers.extend(["Content-Type: application/json", f"Content-Length: {len(payload)}"])
        connection = MemoryConnection("\r\n".join(headers).encode() + b"\r\n\r\n" + payload)
        SandboxHandler(connection, ("127.0.0.1", 49152), self.server)
        response = http.client.HTTPResponse(ResponseConnection(bytes(connection.outgoing)))
        response.begin()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        data = json.loads(raw) if "application/json" in content_type else raw
        return response.status, data, dict(response.getheaders())

    def run_dataset(self, dataset=None, settings=None):
        status, result, _ = self.request("POST", "/api/run", {"dataset": dataset or warehouse(), "settings": settings or {}})
        self.assertEqual(status, 200, result)
        return result

    def test_full_export_json_can_be_reimported_with_saved_scenario_settings(self):
        settings = {"forklift_count": 2, "speed_mps": 1.5, "adoption_percent": 0, "seed": 123}
        saved = self.run_dataset(settings=settings)
        status, exported, _ = self.request("GET", f"/api/runs/{saved['run_id']}/export?format=json")
        self.assertEqual(status, 200)
        status, imported, _ = self.request("POST", "/api/import", {"format": "json", "data": json.dumps(exported)})
        self.assertEqual(status, 200, imported)
        self.assertEqual(imported["dataset"], saved["dataset"])
        self.assertEqual(imported["settings"], saved["settings"])
        rerun = self.run_dataset(imported["dataset"], imported["settings"])
        for branch in ("baseline", "proposed"):
            self.assertEqual(rerun[branch], saved[branch])

    def test_cli_output_wrapper_can_be_reimported_without_run_id(self):
        saved = self.run_dataset(settings={"adoption_percent": 25, "seed": 987})
        wrapper = {key: value for key, value in saved.items() if key not in ("run_id", "created_at", "dataset_summary")}
        status, imported, _ = self.request("POST", "/api/import", {"format": "json", "data": wrapper})
        self.assertEqual(status, 200, imported)
        self.assertEqual(imported["dataset"], saved["dataset"])
        self.assertEqual(imported["settings"], saved["settings"])

    def test_csv_templates_import_and_simulate_with_and_without_optional_history(self):
        status, archive_bytes, _ = self.request("GET", "/api/templates")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            files = {name: archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".csv")}
        self.assertEqual(set(files), {"locations.csv", "docks.csv", "forklifts.csv", "inventory.csv", "orders.csv", "historical_moves.csv"})
        for include_history in (True, False):
            with self.subTest(include_history=include_history):
                selected = dict(files)
                if not include_history:
                    selected.pop("historical_moves.csv")
                status, imported, _ = self.request("POST", "/api/import", {"format": "csv", "files": selected})
                self.assertEqual(status, 200, imported)
                result = self.run_dataset(imported["dataset"])
                self.assertEqual(result["baseline"]["metrics"]["truck_count"], imported["summary"]["orders"])
                self.assertEqual(result["proposed"]["metrics"]["truck_count"], imported["summary"]["orders"])

    def test_partial_csv_selection_names_missing_required_tables(self):
        status, error, _ = self.request("POST", "/api/import", {"format": "csv", "files": {"inventory.csv": csv_files()["inventory.csv"]}})
        self.assertEqual(status, 400)
        details = " ".join(error["details"])
        for filename in ("locations.csv", "docks.csv", "forklifts.csv", "orders.csv"):
            self.assertIn(filename, details)
        self.assertFalse(list(self.server.data_dir.iterdir()))

    def test_json_bom_is_accepted_as_a_normal_file_export(self):
        status, imported, _ = self.request("POST", "/api/import", {"format": "json", "data": "\ufeff" + json.dumps(warehouse())})
        self.assertEqual(status, 200, imported)
        self.assertEqual(imported["summary"]["units"], 21)

    def test_malformed_enum_types_return_actionable_400_not_server_failure(self):
        data = warehouse()
        data["locations"][0]["kind"] = ["storage"]
        status, error, _ = self.request("POST", "/api/import", {"format": "json", "data": data})
        self.assertEqual(status, 400, error)
        self.assertTrue(error["details"])

    def test_zero_adoption_scenario_settings_survive_save_fetch_and_repeat(self):
        first = self.run_dataset(settings={"forklift_count": 2, "adoption_percent": 0, "lookahead_minutes": 15, "seed": 811})
        status, fetched, _ = self.request("GET", f"/api/runs/{first['run_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["settings"], first["settings"])
        self.assertEqual(first["baseline"]["metrics"], first["proposed"]["metrics"])
        self.assertTrue(first["proposed"]["suggestions"])
        self.assertFalse(any(proposal["adopted"] for proposal in first["proposed"]["suggestions"]))
        second = self.run_dataset(fetched["dataset"], fetched["settings"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["baseline"], second["baseline"])
        self.assertEqual(first["proposed"], second["proposed"])

    def test_csv_export_neutralizes_customer_identifier_formulas(self):
        data = warehouse()
        for record in data["inventory"] + data["orders"]:
            if record["owner_id"] == "tenant-a":
                record["owner_id"] = "=DANGEROUS()"
        result = self.run_dataset(data)
        status, raw, _ = self.request("GET", f"/api/runs/{result['run_id']}/export?format=csv")
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        protected = [row for row in rows if row["pallet_id"] == "P1"]
        self.assertTrue(protected)
        self.assertTrue(all(row["owner_id"] == "'=DANGEROUS()" for row in protected))

    def test_unknown_saved_run_returns_a_clear_not_found_error(self):
        status, error, _ = self.request("GET", "/api/runs/" + "0" * 32)
        self.assertEqual(status, 404)
        self.assertIn("not found", error["error"].lower())


if __name__ == "__main__":
    unittest.main()
