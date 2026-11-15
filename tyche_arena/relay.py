"""Loopback relay between Codex and Leadpoet's Responses bridge that selects one Arena-approved model route.

Leadpoet's broker accepts an exact route selector on model requests in full-thinking rounds
(lab_arena/operations.py validate_operation_request: provider = {"only": [route]}, one route from the round's
reasoning registry; the broker then installs every host control itself: no fallbacks, data_collection deny, price
caps, reasoning capture). Codex cannot add a body field, so this relay inserts that selector into each
/v1/responses body and forwards the request unchanged otherwise. It only talks to the bridge on 127.0.0.1, holds
no credential (Codex's own bridge token header is passed through), and returns the bridge's status and body as is.
host.model_route() installs it only when the runner marks the run as full-thinking
(LAB_ARENA_REASONING_CAPTURE_POLICY, the same test lab_arena/shim.py uses for allow_full_thinking).

Route choice (provider probe Nov 9, n=25 per route; local Arena run 33 calls): together median 2.0-2.4 s per call
vs 5.5-5.7 s on fireworks (the selector-absent default), worst 20 s vs 38-74 s; calls seconds apart are
prompt-cached on both, so the cost per call is the same. Without the selector the broker uses its default route,
so the relay falls back to that:
- for the rest of the run when routed calls fail before any routed call has succeeded (a broker refusal of the
  selector reaches Codex as a 502, like an outage, so an early failure streak is treated as a refusal);
- for PAUSE_SECONDS after FAILURES_BEFORE_PAUSE consecutive upstream failures (5xx or a provider 429) later on;
- for the rest of the run when the bridge refuses the selected body locally (4xx) but accepts it without.
A Codex disconnect is passed on: the upstream request is closed, so the bridge's own client-closed checks still see it.
"""

import http.client
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROUTE = "together"
FAILURES_BEFORE_PAUSE = 3
EARLY_FAILURES_BEFORE_REFUSAL = 2
PAUSE_SECONDS = 600
PASSED_HEADERS = ("authorization", "content-type", "accept")
# The bridge's own refusals (deadline, guard, one request at a time) are not provider failures.
BRIDGE_REFUSALS = (b"request unavailable", b"request already in progress")
UNAVAILABLE = b'{"error":{"message":"Arena broker unavailable"}}'

_lock = threading.Lock()
_state = {"failures": 0, "paused_until": 0.0, "refused": False, "succeeded": False}


def route_active():
    with _lock:
        return not _state["refused"] and time.monotonic() >= _state["paused_until"]


def record(status, body):
    upstream_failure = status >= 500 or (status == 429 and not any(text in body for text in BRIDGE_REFUSALS))
    with _lock:
        if 200 <= status < 300:
            _state["failures"] = 0
            _state["succeeded"] = True
        elif upstream_failure:
            _state["failures"] += 1
            if not _state["succeeded"] and _state["failures"] >= EARLY_FAILURES_BEFORE_REFUSAL:
                _state["refused"] = True
            elif _state["failures"] >= FAILURES_BEFORE_PAUSE:
                _state["failures"] = 0
                _state["paused_until"] = time.monotonic() + PAUSE_SECONDS


def with_route(raw):
    """The same JSON body with the route selector as its first field, or None when it cannot take one."""
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or not document or "provider" in document:
        return None
    text = raw.lstrip()
    if not text.startswith(b"{"):
        return None
    # Insert without re-encoding, so every other byte the bridge reads is exactly what Codex sent.
    return b'{"provider":{"only":[' + json.dumps(ROUTE).encode() + b']},' + text[1:]


def _client_closed(connection):
    """The bridge's own test: a waiting client's socket turns readable only when it closes."""
    try:
        readable, _, _ = select.select([connection], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


class Relay:
    def __init__(self, target):
        parts = urlsplit(target)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
            raise ValueError("the relay forwards only to the local bridge")
        self.host, self.port, self.prefix = parts.hostname, parts.port, parts.path.rstrip("/")
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                relay.forward(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d%s" % (self.server.server_port, self.prefix)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def send(self, path, headers, body, client):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=None)
        done = threading.Event()

        def watch():
            # Close the upstream request when Codex goes away, as a direct client's close would.
            while not done.wait(0.5):
                if _client_closed(client):
                    try:
                        if connection.sock is not None:
                            connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        try:
            connection.request("POST", path, body=body, headers={**headers, "Content-Length": str(len(body))})
            watcher.start()
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type") or "application/json", response.read()
        except (OSError, http.client.HTTPException):
            return 502, "application/json", UNAVAILABLE
        finally:
            done.set()
            connection.close()

    def forward(self, handler):
        try:
            size = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        raw = handler.rfile.read(size) if size > 0 else b""
        headers = {key: value for key, value in handler.headers.items() if key.lower() in PASSED_HEADERS}
        routed = with_route(raw) if handler.path == self.prefix + "/responses" and route_active() else None
        client = handler.connection
        status, content_type, reply = self.send(handler.path, headers, routed if routed is not None else raw, client)
        if routed is not None:
            if status in (400, 404, 422) and not _client_closed(client):
                # The bridge's own body checks run before any paid dispatch; resend the original body once.
                plain = self.send(handler.path, headers, raw, client)
                if 200 <= plain[0] < 300:
                    with _lock:
                        _state["refused"] = True
                status, content_type, reply = plain
            elif not _client_closed(client):
                # A request Codex abandoned says nothing about the route.
                record(status, reply)
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(reply)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(reply)
        except OSError:
            pass
