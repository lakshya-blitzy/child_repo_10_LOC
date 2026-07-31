"""Greeting helper plus a read-only ``GET /health`` endpoint.

Importing this module has no side effect. Run it with no arguments and it
prints the greeting exactly as it always has; run it with ``--serve`` and it
starts an HTTP listener that answers ``GET`` and ``HEAD`` on ``/health`` with a
compact JSON document reporting the application name, version, the current UTC
instant and a status of ``UP``. The optional ``HOST`` and ``PORT`` environment
variables override the bind address.

The listener is built on :mod:`http.server`, which the standard library
documents as not recommended for production. That caveat is mitigated here by
defaulting the bind address to loopback and by keeping the response body to the
four documented fields, so nothing about the host or the runtime is disclosed.
"""

import json
import os
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "child_repo_10_LOC"
APP_VERSION = "1.0.0"
HEALTH_PATH = "/health"
# Emitted verbatim as the ``Allow`` header value. The sibling implementations
# emit the identical string, so the exact ", " spacing is part of the contract.
ALLOWED_METHODS = "GET, HEAD"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def greet(name):
    return f"Hello {name}"


def current_timestamp():
    """Return the current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def health_payload():
    """Build the health document as a fresh dict in wire key order."""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "timestamp": current_timestamp(),
        "status": "UP",
    }


class HealthRequestHandler(BaseHTTPRequestHandler):
    """Answer ``GET`` and ``HEAD`` on ``/health``, always in JSON."""

    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{APP_VERSION}"
    # Suppresses the interpreter version banner the base class would otherwise
    # advertise in the ``Server`` header of every response.
    sys_version = ""

    def do_GET(self):
        """Serve the health document."""
        self._route()

    def do_HEAD(self):
        """Serve the health headers; RFC 9110 expects HEAD alongside GET."""
        self._route()

    def send_error(self, code, message=None, explain=None):
        """Keep every error response JSON and free of request-derived text.

        Overriding the base implementation is what stops an unsupported verb
        receiving the stock ``501`` HTML page, whose body repeats the request
        method back to the caller. ``message`` and ``explain`` are ignored for
        exactly that reason: the reason phrase is derived from the status code
        alone, never from the request.
        """
        status = code
        if status == HTTPStatus.NOT_IMPLEMENTED:
            # An unrecognised verb is a method this endpoint refuses, not a gap
            # in the server, so it is reported with the permitted methods.
            status = HTTPStatus.METHOD_NOT_ALLOWED
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            # A non-standard code still yields a safe fixed string.
            reason = "Error"
        extra_headers = None
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            extra_headers = {"Allow": ALLOWED_METHODS}
        self._send_json(status, {"error": reason}, extra_headers)

    def _route(self):
        """Dispatch on the request path; GET and HEAD both land here."""
        # Query and fragment are stripped so /health?probe=lb still matches.
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path != HEALTH_PATH:
            # Fixed literal body: the requested path is never reflected back.
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": HTTPStatus.NOT_FOUND.phrase}
            )
            return
        self._send_json(HTTPStatus.OK, health_payload())

    def _send_json(self, status, body, extra_headers=None):
        """Write ``body`` as compact JSON with an accurate Content-Length."""
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # The timestamp is generated per request, so a cached liveness answer
        # would be worse than none at all.
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for header, value in extra_headers.items():
                self.send_header(header, value)
        self.end_headers()
        # HEAD carries the GET headers, Content-Length included, but no body.
        if self.command != "HEAD":
            self.wfile.write(payload)


def create_server(host=None, port=None):
    """Return an unstarted server bound to ``host`` and ``port``.

    Each value is taken from the explicit argument, else the environment, else
    the module default. The arguments are tested with ``is None`` rather than
    for truthiness so an explicit ``port=0`` reaches the socket and yields an
    ephemeral port, which is how a test suite binds without a fixed port.
    """
    if host is None:
        # An unset or empty HOST keeps the loopback default rather than
        # exposing the listener on every interface.
        host = os.environ.get("HOST") or DEFAULT_HOST
    if port is None:
        try:
            # An unset, empty or malformed PORT falls back to the default
            # instead of raising at start-up.
            port = int(os.environ.get("PORT", ""))
        except ValueError:
            port = DEFAULT_PORT
        if not 0 <= port <= 65535:
            port = DEFAULT_PORT
    return ThreadingHTTPServer((host, port), HealthRequestHandler)


def serve():
    """Bind the health endpoint and serve it until interrupted."""
    server = create_server()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}{HEALTH_PATH}"
    # flush=True: stdout is block-buffered when redirected, and the banner must
    # not queue up behind the first access-log line.
    print(f"{APP_NAME} {APP_VERSION} listening on {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    if "--serve" in sys.argv[1:]:
        serve()
    else:
        user = "Lakshya"
        print(greet(user))
