import json
import unittest
from unittest.mock import patch

from app import app


class HostedUserFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_demo_run_and_reimport_without_server_storage(self):
        for path in ["/", "/app.js", "/styles.css", "/api/health", "/api/templates"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)
        demo = self.client.get("/api/demo").get_json()
        with patch("warehouse_sandbox.server.tempfile.NamedTemporaryFile", side_effect=AssertionError("No hosted file writes")):
            response = self.client.post("/api/run", json={"dataset": demo, "settings": {}})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["storage_mode"], "browser")
        self.assertEqual(result["policy"]["id"], "original_optimizer_phase1")
        self.assertEqual(result["proposed"]["metrics"]["truck_count"], 8)
        restored = self.client.post("/api/import", json={"data": result}).get_json()
        self.assertEqual(restored["dataset"], result["dataset"])
        self.assertEqual(self.client.get("/api/runs/" + result["run_id"]).status_code, 410)

    def test_origin_and_input_boundaries(self):
        self.assertEqual(self.client.post("/api/run", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/run", json={}, headers={"Origin": "https://localhost"}).status_code, 400)
        self.assertEqual(self.client.post("/api/import", data="x", content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.post("/api/import", data="not-json", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/import", data=b"x" * 4_000_001, content_type="application/json").status_code, 413)
