"""
Local loopback HTTP server for desktop login (see app.py's module docstring for the full flow).

Google/GitHub actively block or challenge OAuth attempted from an embedded WebView - exactly
what this app's pywebview window is - so login now happens in the user's real system browser
(webbrowser.open()) instead of inside the WebView. This tiny server is how the result gets back
to this process: Local's own auth_callback (P07 desktop-login branch, browseterm-server-local's
api_handlers.py) redirects the system browser here with a one-time device-bootstrap code once
OAuth and the Local login itself complete.

Deliberately NOT a general-purpose HTTP server: exactly one route (GET /callback), binds only to
127.0.0.1:0 (an OS-assigned free port, never 0.0.0.0 - this must never be reachable from the
network, and a stale process from a previous run can never block a fresh attempt), and serves at
most one real request before shutting itself down.
"""
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

_SUCCESS_HTML = b"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>BrowseTerm</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;background:#A8FBD3;}
.card{background:white;padding:2rem 2.5rem;border-radius:8px;text-align:center;
box-shadow:0 2px 10px rgba(0,0,0,0.1);}</style></head>
<body><div class="card"><h1>You're logged in</h1><p>You can close this tab and return to
BrowseTerm.</p></div></body></html>"""

_ERROR_HTML = b"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>BrowseTerm</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;background:#A8FBD3;}
.card{background:white;padding:2rem 2.5rem;border-radius:8px;text-align:center;
box-shadow:0 2px 10px rgba(0,0,0,0.1);}</style></head>
<body><div class="card"><h1>Login didn't complete</h1><p>Please return to BrowseTerm and try
again.</p></div></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    # Set per-subclass by LoopbackServer (one HTTPServer -> one handler class -> one queue), so
    # a stray second request can never cross-talk between two LoopbackServer instances.
    result_queue: "queue.Queue[Optional[str]]"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        code = parse_qs(parsed.query).get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_SUCCESS_HTML if code else _ERROR_HTML)
        self.result_queue.put(code)

    def log_message(self, format: str, *args) -> None:
        pass  # silence BaseHTTPRequestHandler's default stderr access log


class LoopbackServer:
    """One-shot loopback server. Construct it, read .port to build the redirect URL Local needs,
    start() it, then wait_for_code() (blocking, with a timeout) for the one request it will ever
    actually handle."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1)
        handler_cls = type("_BoundCallbackHandler", (_CallbackHandler,), {"result_queue": self._queue})
        self._httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._httpd.handle_request, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def wait_for_code(self, timeout_seconds: float) -> Optional[str]:
        """Blocks for the callback (or the timeout, whichever comes first). Always closes the
        server's listening socket before returning, whether or not a code arrived."""
        try:
            return self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        finally:
            self._httpd.server_close()
