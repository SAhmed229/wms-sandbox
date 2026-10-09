"""Vercel WSGI entrypoint; reuse the local API without persistent server files."""
import io
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlsplit

from flask import Flask, Response, request
from warehouse_sandbox.server import SandboxHandler, json_bytes

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4_000_000


class HostedHandler(SandboxHandler):
    def _send(self, payload, status=200, content_type="application/json; charset=utf-8", filename=None):
        if content_type.startswith("text/html"):
            payload = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            payload = payload.replace("Local sandbox · recommendations only", "Hosted sandbox · recommendations only")
            payload = payload.replace("Maximum request size: 5 MB.", "Maximum request size: 4 MB.")
            payload = payload.replace("Files are processed by the local server and saved with each run on this computer.", "Files are processed on the hosted server for this request. Runs are not saved there. Download results before refreshing or closing this page.")
        if isinstance(payload, (dict, list)):
            payload = json_bytes(payload)
        self.response = Response(payload, status=status, content_type=content_type)
        self.response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })
        if filename:
            self.response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def _host_ok(self):
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"https", "http"} or parsed.netloc != request.host:
                self._error("Cross-origin access is disabled.", 403)
                return False
        return True

    def _store_run(self, result):
        result["storage_mode"] = "browser"
        result["warnings"].append("Hosted runs are not saved on the server. Download the result JSON before closing or refreshing this page.")
        return json_bytes(result)


@app.errorhandler(413)
def too_large(error):
    return {"error": "Hosted uploads must be smaller than 4 MB.", "details": []}, 413


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "OPTIONS"])
def dispatch(path):
    handler = object.__new__(HostedHandler)
    handler.path = request.full_path.rstrip("?")
    handler.headers = Message()
    for key, value in request.headers.items():
        handler.headers[key] = value
    handler.rfile = io.BytesIO(request.get_data())
    handler.connection = SimpleNamespace(settimeout=lambda seconds: None)
    if path.startswith("api/runs/"):
        handler._error("Hosted runs are not stored on the server. Import your downloaded result JSON and run a new comparison.", 410)
    else:
        getattr(handler, "do_" + request.method)()
    return handler.response
