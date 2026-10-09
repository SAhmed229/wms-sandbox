import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from warehouse_sandbox.importer import LIMITS
from warehouse_sandbox.server import SandboxServer
from tests_sandbox.fixtures import warehouse


class LocalAPIBoundaryTests(unittest.TestCase):
    """Exercise real local HTTP requests without warehouse or internet services."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="warehouse-sandbox-test-")
        cls.server = SandboxServer(("127.0.0.1", 0), cls.directory.name)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        payload = json.dumps(body) if body is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            result = json.loads(raw) if "application/json" in content_type else raw.decode("utf-8")
            return response.status, result, dict(response.getheaders())
        finally:
            connection.close()

    def test_health_identifies_suggestions_only_mode(self):
        status, health, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["mode"], "historical_simulation")
        self.assertIs(health["production_connections"], False)

    def test_import_validates_without_persisting_customer_data(self):
        before = set(Path(self.directory.name).iterdir())
        status, result, _ = self.request("POST", "/api/import", {"format": "json", "data": warehouse()})
        self.assertEqual(status, 200)
        self.assertEqual(result["summary"]["units"], 21)
        self.assertEqual(set(Path(self.directory.name).iterdir()), before)

    def test_saved_run_roundtrips_and_exports_local_results(self):
        status, result, _ = self.request("POST", "/api/run", {"dataset": warehouse(), "settings": {"adoption_percent": 100}})
        self.assertEqual(status, 200)
        run_id = result["run_id"]
        self.assertTrue((Path(self.directory.name) / (run_id + ".json")).is_file())
        status, fetched, _ = self.request("GET", "/api/runs/" + run_id)
        self.assertEqual(status, 200)
        self.assertEqual(fetched, result)
        status, downloaded, headers = self.request("GET", "/api/runs/" + run_id + "/export?format=json")
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, result)
        self.assertIn("attachment", headers["Content-Disposition"])
        status, suggestions, headers = self.request("GET", "/api/runs/" + run_id + "/export?format=csv")
        self.assertEqual(status, 200)
        self.assertIn("pallet_id", suggestions.splitlines()[0])
        self.assertIn("P1", suggestions)
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_invalid_import_returns_details_without_creating_a_run(self):
        before = set(Path(self.directory.name).iterdir())
        data = warehouse()
        data["orders"][0]["owner_id"] = "another-owner"
        status, error, _ = self.request("POST", "/api/run", {"dataset": data})
        self.assertEqual(status, 400)
        self.assertTrue(error["details"])
        self.assertEqual(set(Path(self.directory.name).iterdir()), before)

    def test_old_saved_comparisons_are_flagged_for_recalculation(self):
        status, result, _ = self.request("POST", "/api/run", {"dataset": warehouse()})
        self.assertEqual(status, 200)
        saved_file = Path(self.directory.name) / (result["run_id"] + ".json")
        result.pop("model_version")
        saved_file.write_text(json.dumps(result))
        status, reopened, _ = self.request("GET", "/api/runs/" + result["run_id"])
        self.assertEqual(status, 200)
        self.assertTrue(any("predates the replay fixes" in warning for warning in reopened["warnings"]))

    def test_cross_origin_and_rebound_hosts_cannot_read_local_data(self):
        for headers in ({"Origin": "https://unrelated.example"}, {"Host": "unrelated.example"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("GET", "/api/health", headers=headers)
                self.assertEqual(status, 403)

    def test_no_dispatch_writeback_or_arbitrary_file_endpoints(self):
        for path in ("/api/dispatch", "/api/writeback", "/api/wms/connect"):
            with self.subTest(path=path):
                status, _, _ = self.request("POST", path, {})
                self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/runs/../../etc/passwd")
        self.assertEqual(status, 404)

    def test_request_size_is_rejected_before_import(self):
        status, _, _ = self.request("POST", "/api/import", {}, {"Content-Length": str(LIMITS["request_bytes"] + 1)})
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
