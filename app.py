import json
import os
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "child_repo_10_LOC"
APP_VERSION = "1.0.0"
HEALTH_PATH = "/health"
# The sibling implementations emit the identical string, so the exact ", "
# spacing is part of the contract.
ALLOWED_METHODS = "GET, HEAD"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def greet(name):
    return f"Hello {name}"


def current_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def health_payload():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "timestamp": current_timestamp(),
        "status": "UP",
    }


class HealthRequestHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{APP_VERSION}"
    # Suppresses the interpreter version banner the base class would otherwise
    # advertise in the ``Server`` header of every response.
    sys_version = ""

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    # Returning True without writing anything skips the interim 100 Continue
    # the base class would send as soon as the header was parsed, granting an
    # upload before the path and the method had been looked at. The routed
    # answer goes out instead, and closing discards whatever still arrives.
    def handle_expect_100(self):
        return True

    # Discards the access log, the one sink that would see raw request text:
    # the base class copies the request line to stderr verbatim, so a probe of
    # /health?token=... would write caller-supplied data, control characters
    # included, into the operator's log (CWE-532).
    def log_message(self, fmt, *args):
        pass

    # Overriding this is what stops an unsupported verb receiving the stock 501
    # HTML page, whose body repeats the request method back to the caller.
    # ``message`` and ``explain`` are ignored for the same reason: the reason
    # phrase is derived from the status code alone, never from the request.
    def send_error(self, code, message=None, explain=None):
        status = code
        if status == HTTPStatus.NOT_IMPLEMENTED:
            # The base class answers a missing do_* handler with 501 before
            # any routing happens, so the path decision is repeated here to
            # keep both entry points in the same order, path first. Every
            # other status came from a line that never parsed, so it stands.
            if self._request_path() == HEALTH_PATH:
                status = HTTPStatus.METHOD_NOT_ALLOWED
            else:
                status = HTTPStatus.NOT_FOUND
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            # A non-standard code still yields a safe fixed string.
            reason = "Error"
        extra_headers = None
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            extra_headers = {"Allow": ALLOWED_METHODS}
        self._send_json(status, {"error": reason}, extra_headers)

    # The target comes from the raw request line, not from ``path``: the base
    # class collapses a leading run of slashes there, so //health would arrive
    # as /health and be answered 200 where the siblings answer 404. The line is
    # split as the base class splits it and the target is its second field; a
    # line that never parsed leaves none, which matches no route.
    def _request_path(self):
        line = getattr(self, "requestline", None)
        if not isinstance(line, str):
            return None
        fields = line.split()
        if not 2 <= len(fields) <= 3:
            return None
        # Query and fragment are stripped so /health?probe=lb still matches.
        # Nothing else is: the target is never percent-decoded, so no encoded
        # spelling of the route is mistaken for the route.
        return fields[1].split("?", 1)[0].split("#", 1)[0]

    def _route(self):
        if self._request_path() != HEALTH_PATH:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": HTTPStatus.NOT_FOUND.phrase}
            )
            return
        self._send_json(HTTPStatus.OK, health_payload())

    def _send_json(self, status, body, extra_headers=None):
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        # The base class suppresses the status line and every header while the
        # recorded version is HTTP/0.9, which it still is for a request line it
        # rejected before reading the version - "GET /health HTTP/2.0" among
        # them. Answering in HTTP/1.1 keeps every response a complete message
        # instead of a naked body no caller could attribute to a status.
        if self.request_version == "HTTP/0.9":
            self.request_version = self.protocol_version
        # Every response ends its connection, which is the policy all three
        # applications of this composition share: this endpoint reads no
        # request body, and a connection reused while holding unread body
        # bytes lets those bytes be parsed as the next request (CWE-444). The
        # attribute is what the base class's request loop reads, so it is set
        # here as well as announced in the header below.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # The timestamp is generated per request, so a cached liveness answer
        # would be worse than none at all.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if extra_headers:
            for header, value in extra_headers.items():
                self.send_header(header, value)
        self.end_headers()
        # HEAD carries the GET headers, Content-Length included, but no body.
        if self.command != "HEAD":
            self.wfile.write(payload)


# Each argument is tested with ``is None`` rather than for truthiness, so an
# explicit ``port=0`` reaches the socket and yields an ephemeral port.
def create_server(host=None, port=None):
    if host is None:
        # The environment value is normalised before it reaches the socket: an
        # unset, empty or whitespace-only HOST keeps the loopback default
        # rather than exposing the listener on every interface or failing to
        # bind at all, and a padded value is trimmed to the address it names.
        configured_host = os.environ.get("HOST", "").strip()
        host = configured_host or DEFAULT_HOST
    if port is None:
        # An unset, blank, non-numeric or out-of-range PORT falls back to the
        # default instead of raising at start-up. Only ASCII digits pass, so
        # the siblings resolve the same value from the same string: int() alone
        # would also take "+1234", "1_234" and non-ASCII digits, which they do
        # not.
        configured_port = os.environ.get("PORT", "").strip()
        if configured_port and all(
            character in "0123456789" for character in configured_port
        ):
            port = int(configured_port)
        else:
            port = DEFAULT_PORT
        if port > 65535:
            port = DEFAULT_PORT
    return ThreadingHTTPServer((host, port), HealthRequestHandler)


def serve():
    try:
        server = create_server()
    except OSError:
        # A bind failure would otherwise print a traceback carrying absolute
        # repository paths and standard-library internals, so it is reported as
        # one fixed sentence naming the variables to check, never their values.
        print(
            f"{APP_NAME} {APP_VERSION} could not bind the health endpoint;"
            " check HOST and PORT",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}{HEALTH_PATH}"
    # flush=True: stdout is block-buffered when redirected, so without it the
    # banner would sit in the buffer while the endpoint was already answering.
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
