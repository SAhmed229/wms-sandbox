"""Loopback-only local UI and API for historical replay. Standard library only."""

from __future__ import annotations

import argparse
import csv
import errno
import http.client
import io
import json
import logging
import os
import re
import tempfile
import threading
import uuid
import zipfile
import structlog
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .engine import MODEL_VERSION
from .optimizer_bridge import POLICY_METADATA, _load_optimizer
from .importer import CSV_TABLES, DEFAULT_SETTINGS, LIMITS, ValidationError, import_csv, normalize_json_import, normalize_settings, summarize, validate_dataset

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "warehouse_sandbox" / "static"
DEMO = ROOT / "examples" / "sandbox" / "demo.json"
RUN_LOCK = threading.Lock()
LOGGER = logging.getLogger("warehouse_sandbox")


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class SandboxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_dir):
        super().__init__(address, SandboxHandler)
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)


class SandboxHandler(BaseHTTPRequestHandler):
    server_version = "WarehouseReplay/0.1"

    def log_message(self, fmt, *args):
        LOGGER.info(fmt, *args)

    def _send(self, payload, status=200, content_type="application/json; charset=utf-8", filename=None):
        if isinstance(payload, (dict, list)):
            payload = json_bytes(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def _store_run(self, result):
        encoded = json_bytes(result)
        with tempfile.NamedTemporaryFile(dir=self.server.data_dir, prefix=".run-", suffix=".tmp", delete=False) as file:
            file.write(encoded)
            temp_path = Path(file.name)
        os.replace(temp_path, self.server.data_dir / (result["run_id"] + ".json"))
        return encoded

    def _error(self, message, status=400, details=None):
        self._send({"error": message, "details": details or []}, status)

    def _host_ok(self):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._error("Use the loopback address shown when the sandbox starts.", 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self._error("Cross-origin access is disabled for the local sandbox.", 403)
            return False
        return True

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        path = url.path
        if path in {"/", "/index.html", "/app.js", "/styles.css"}:
            filename = "index.html" if path == "/" else path[1:]
            target = STATIC / filename
            if not target.exists():
                return self._error("UI is not available.", 404)
            content_type = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[target.suffix[1:]]
            return self._send(target.read_bytes(), content_type=content_type + "; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(b"", 204, "image/x-icon")
        if path == "/api/health":
            return self._send({"status": "ok", "mode": "historical_simulation", "production_connections": False, "model_version": MODEL_VERSION, "policy": POLICY_METADATA})
        if path in {"/api/demo", "/api/demo/download"}:
            return self._send(json.loads(DEMO.read_text()), filename="warehouse-demo.json" if path.endswith("download") else None)
        if path == "/api/schema":
            return self._send({"dataset_example": json.loads(DEMO.read_text()), "csv_tables": CSV_TABLES,
                               "settings": DEFAULT_SETTINGS, "limits": LIMITS})
        if path == "/api/templates":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in sorted((ROOT / "examples" / "sandbox" / "csv").glob("*.csv")):
                    archive.writestr(file.name, file.read_bytes())
                archive.writestr("README.txt", "Replace the synthetic demo rows with your authorized export.\nSelect all six CSV files in the app together. historical_moves.csv can be omitted.\nCoordinates: metres. Times: seconds since the facility start time.\norders.csv repeats order metadata once for every pallet line.\nOne inventory record is one uniquely identified physical pallet; quantity counts units.\n")
            return self._send(buffer.getvalue(), content_type="application/zip", filename="warehouse-csv-templates.zip")
        match = re.fullmatch(r"/api/runs/([a-f0-9]{32})(/export)?", path)
        if match:
            target = self.server.data_dir / (match[1] + ".json")
            if not target.exists():
                return self._error("Run not found.", 404)
            result = json.loads(target.read_text())
            if result.get("model_version") != MODEL_VERSION:
                result["warnings"] = list(result.get("warnings", [])) + ["This saved comparison predates the replay fixes. Run a new comparison to update its estimates."]
            if not match[2]:
                return self._send(result)
            fmt = parse_qs(url.query).get("format", ["json"])[0]
            if fmt == "json":
                return self._send(result, filename=f"warehouse-run-{match[1][:8]}.json")
            if fmt == "csv":
                fields = ["id", "at_seconds", "pallet_id", "sku_id", "owner_id", "order_id", "from_location_id", "to_location_id", "dock_id", "estimated_load_travel_saved_seconds", "move_cost_seconds", "score", "score_components", "policy_source", "adopted", "reason"]
                output = io.StringIO(newline="")
                writer = csv.DictWriter(output, fields, extrasaction="ignore")
                writer.writeheader()
                for row in result["proposed"]["suggestions"]:
                    row = {**row, "score_components": json.dumps(row.get("score_components", {}), sort_keys=True)}
                    safe = {key: ("'" + val if isinstance(val, str) and val.startswith(("=", "+", "-", "@", "\t", "\r")) else val) for key, val in row.items()}
                    writer.writerow(safe)
                return self._send(output.getvalue(), content_type="text/csv; charset=utf-8", filename=f"suggestions-{match[1][:8]}.csv")
            return self._error("Export format must be json or csv.")
        self._error("Endpoint not found.", 404)

    def do_POST(self):
        if not self._host_ok():
            return
        if self.headers.get_content_type() != "application/json":
            return self._error("Send JSON with Content-Type application/json.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error("Invalid Content-Length.")
        if not 0 < length <= LIMITS["request_bytes"]:
            return self._error("Request must contain JSON and be at most 5 MB.", 413)
        self.connection.settimeout(15)
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                return self._error("Request must be a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/import":
                restored_settings = None
                if body.get("format", "json") == "csv":
                    dataset = import_csv(body.get("files"), body.get("facility_name", "Imported warehouse"),
                                         body.get("start_time", "2026-09-14T06:00:00-04:00"))
                elif body.get("format", "json") == "json":
                    dataset, restored_settings = normalize_json_import(body.get("data"))
                else:
                    return self._error("Import format must be json or csv.")
                response = {"dataset": dataset, "summary": summarize(dataset), "warnings": []}
                if restored_settings is not None:
                    response["settings"] = restored_settings
                    response["warnings"].append("Imported an exported run's warehouse data and restored its scenario settings. Run the simulation to calculate a new comparison.")
                return self._send(response)
            if path == "/api/run":
                dataset = validate_dataset(body.get("dataset"))
                settings = normalize_settings(body.get("settings"), dataset)
                if not RUN_LOCK.acquire(blocking=False):
                    return self._error("A replay is already running. Try again when it finishes.", 409)
                try:
                    from .engine import run_comparison
                    result = run_comparison(dataset, settings)
                    result.update({"run_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                                   "dataset": dataset, "dataset_summary": summarize(dataset)})
                    encoded = self._store_run(result)
                    return self._send(encoded)
                finally:
                    RUN_LOCK.release()
            return self._error("Endpoint not found.", 404)
        except ValidationError as exc:
            self._error(str(exc), 400, exc.details)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error("Invalid JSON. Check file syntax and UTF-8 encoding.")
        except (TimeoutError, ConnectionError):
            self._error("Upload timed out.", 408)
        except Exception:
            LOGGER.exception("Replay request failed")
            self._error("Replay could not complete. See the local terminal for diagnostics.", 500)

    def do_OPTIONS(self):
        self._error("Cross-origin access is disabled.", 403)


def main():
    parser = argparse.ArgumentParser(description="Start the local suggestions-only warehouse replay sandbox.")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--data-dir", default=str(ROOT / "runs"), help="Local directory for saved replay results")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be from 1 to 65535")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    _load_optimizer()
    try:
        server = SandboxServer(("127.0.0.1", args.port), args.data_dir)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=2)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            health = json.loads(response.read())
            if response.status == 200 and isinstance(health, dict) and health.get("mode") == "historical_simulation" and health.get("production_connections") is False:
                print(f"Warehouse Replay is already running: http://127.0.0.1:{args.port}", flush=True)
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        parser.exit(2, f"Port {args.port} is in use by another application. Choose an unused port with --port.\n")
    print(f"Warehouse Replay is ready: http://127.0.0.1:{args.port}", flush=True)
    print(f"Suggestions and simulation only. Saved local runs: {server.data_dir}", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
