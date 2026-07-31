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
    # the base class sends as soon as the header is parsed, which would grant
    # an upload before the path and the method had been looked at. Routing
    # answers straight away instead, and that response closes the connection,
    # so a body the client may still send is discarded rather than read.
    def handle_expect_100(self):
        return True

    # Discards the access log, the one sink that sees raw request text: the
    # base class writes the request line -- method, path and query string
    # exactly as they arrived -- to stderr, so a probe of /health?token=...
    # would copy caller-supplied data, control characters included, into the
    # operator's log (CWE-532). Nothing here writes to stdout, so the startup
    # banner and the default program's greeting are unaffected.
    def log_message(self, fmt, *args):
        pass

    # Overriding this is what stops an unsupported verb receiving the stock 501
    # HTML page, whose body repeats the request method back to the caller.
    # ``message`` and ``explain`` are ignored for the same reason: the reason
    # phrase is derived from the status code alone, never from the request.
    def send_error(self, code, message=None, explain=None):
        status = code
        if status == HTTPStatus.NOT_IMPLEMENTED:
            # An unrecognised verb never reaches _route, because the base class
            # answers a missing do_* handler with 501 before any routing
            # happens, so the path decision is repeated here to keep both
            # entry points in the same order -- path first, method second, as
            # the siblings do. Every other status arrives from a request line
            # the base class could not parse, where no trustworthy path
            # exists, so those keep the code they came with.
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

    # ``path`` is assigned only once a request line has been accepted, so an
    # error raised while parsing that line can reach send_error before the
    # attribute exists; a missing value matches no route and resolves to the
    # 404 envelope.
    def _request_path(self):
        raw = getattr(self, "path", None)
        if not isinstance(raw, str):
            return None
        # Query and fragment are stripped so /health?probe=lb still matches.
        return raw.split("?", 1)[0].split("#", 1)[0]

    def _route(self):
        if self._request_path() != HEALTH_PATH:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": HTTPStatus.NOT_FOUND.phrase}
            )
            return
        self._send_json(HTTPStatus.OK, health_payload())

    def _send_json(self, status, body, extra_headers=None):
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        # Every response ends its connection. This endpoint reads no request
        # body, and a persistent connection left holding unread body bytes
        # lets those bytes be parsed as the next request on the same stream,
        # so a POST whose body spelled out a GET /health drew two answers
        # where one was asked for (CWE-444). The attribute is what the base
        # class's request loop actually reads, so it is set here as well as
        # announced in the header below.
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
    try:
        server = create_server()
    except OSError:
        # A bind failure -- an unresolvable HOST, an address this machine does
        # not own, a port already in use -- would otherwise print a traceback
        # carrying absolute repository paths and standard-library internals,
        # so it is reported as one fixed sentence naming the variables to
        # check, never their values.
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
