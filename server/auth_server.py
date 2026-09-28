"""Local HTTP server for receiving auth tokens from the browser."""

import threading
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .logger import log

SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head><title>AvaCapo</title></head>
<body style="background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;
display:flex;justify-content:center;align-items:center;height:100vh;margin:0">
<div style="text-align:center">
<h1 style="color:#4ade80">Connected!</h1>
<p>You can close this tab and return to Blender.</p>
</div>
</body>
</html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles the GET /callback?token=XXX request."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            token = params.get("token", [None])[0]
            if token:
                self.server.received_token = token
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(SUCCESS_HTML.encode())
                # Shut down after receiving the token
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight for no-cors fetch."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, format, *args):
        log.debug(f"Auth server: {format % args}")


class PluginAuthServer:
    """Single-use local HTTP server for receiving a token from the browser."""

    def __init__(self):
        self._server = None
        self._thread = None
        self.token = None
        self.port = None
        self.is_running = False

    def start(self) -> int:
        """Start the server on a random available port. Returns the port."""
        # Find a free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        self._server = HTTPServer(("127.0.0.1", self.port), _CallbackHandler)
        self._server.received_token = None
        self._server.timeout = 300  # 5 minute timeout

        self.is_running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        log.info(f"Auth server started on port {self.port}")
        return self.port

    def _run(self):
        try:
            self._server.serve_forever()
            self.token = self._server.received_token
        except Exception as e:
            log.error(f"Auth server error: {e}")
        finally:
            self.is_running = False
            log.info("Auth server stopped")

    def stop(self):
        """Stop the server if still running."""
        if self._server and self.is_running:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self.is_running = False
