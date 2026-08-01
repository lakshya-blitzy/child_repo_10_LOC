import json
import os
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "child_repo_10_LOC"
APP_VERSION = "1.0.0"
HEALTH_PATH = "/health"
ALLOWED_METHODS = "GET, HEAD"
HTTP_1_1 = "HTTP/1.1"
HOST_FIELD = "Host"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
# The digit bound below is what keeps an oversized PORT away from int(), which
# refuses a long enough digit run and would abort start-up with a traceback
# instead of applying the documented fallback.
MAX_PORT = 65535
MAX_PORT_DIGITS = len(str(MAX_PORT))


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

    protocol_version = HTTP_1_1
    server_version = f"{APP_NAME}/{APP_VERSION}"
    # Suppresses the interpreter version banner the base class would otherwise
    # advertise in the ``Server`` header of every response.
    sys_version = ""

    # A peer can vanish while the base class is still reading, which raises
    # where no response-path guard can see it; port scans and probes do exactly
    # that, and each would otherwise print a peer address and a traceback to
    # stderr. Only the peer-disconnect family is caught, so a defect surfaces.
    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            self.close_connection = True

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    # Skips the interim 100 Continue the base class would send as soon as the
    # header was parsed, granting an upload before the route had been looked at.
    def handle_expect_100(self):
        return True

    # The base class copies the request line to stderr verbatim, so a probe of
    # /health?token=... would write caller-supplied data, control characters
    # included, into the operator's log (CWE-532).
    def log_message(self, fmt, *args):
        pass

    # Overriding this is what stops an unsupported verb receiving the stock 501
    # HTML page, whose body repeats the request method back to the caller.
    # ``message`` and ``explain`` are ignored for the same reason.
    def send_error(self, code, message=None, explain=None):
        status = code
        if status == HTTPStatus.NOT_IMPLEMENTED:
            # The base class answers a missing do_* handler with 501 before any
            # routing happens, so the decision is repeated here in the same
            # order -- Host, then path, then the method that arrived. Every
            # other status came from a line that never parsed, so it stands.
            if self._lacks_host():
                status = HTTPStatus.BAD_REQUEST
            elif self._request_path() == HEALTH_PATH:
                status = HTTPStatus.METHOD_NOT_ALLOWED
            else:
                status = HTTPStatus.NOT_FOUND
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Error"
        extra_headers = None
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            extra_headers = {"Allow": ALLOWED_METHODS}
        self._send_json(status, {"error": reason}, extra_headers)

    # From the raw request line, not from ``path``: the base class collapses a
    # leading run of slashes there, so //health would arrive as /health and be
    # answered 200 where the siblings answer 404.
    def _request_path(self):
        line = getattr(self, "requestline", None)
        if not isinstance(line, str):
            return None
        fields = line.split()
        if not 2 <= len(fields) <= 3:
            return None
        # Query and fragment are stripped so /health?probe=lb still matches;
        # nothing else is, so no encoded spelling is mistaken for the route.
        return fields[1].split("?", 1)[0].split("#", 1)[0]

    # ``http.server`` does not enforce the ``Host`` field RFC 9112 requires of
    # an HTTP/1.1 message, so the application does, before the target is looked
    # at. ``headers`` is read through getattr because the response path is
    # reachable without a parsed request, and an absent header set is not
    # evidence the field was omitted. The lookup is case-insensitive, as
    # RFC 9110 compares field names.
    def _lacks_host(self):
        if self.request_version != HTTP_1_1:
            return False
        headers = getattr(self, "headers", None)
        if headers is None:
            return False
        return headers.get(HOST_FIELD) is None

    def _route(self):
        if self._lacks_host():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": HTTPStatus.BAD_REQUEST.phrase},
            )
            return
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
        # rejected before reading the version. Answering in HTTP/1.1 keeps every
        # response a complete message rather than a naked body.
        if self.request_version == "HTTP/0.9":
            self.request_version = self.protocol_version
        # This endpoint reads no request body, and a connection reused while
        # holding unread body bytes lets those bytes be parsed as the next
        # request (CWE-444). The attribute is what the request loop reads, so it
        # is set here as well as announced in the header below.
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            # A per-request timestamp makes a cached answer worse than none.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if extra_headers:
                for header, value in extra_headers.items():
                    self.send_header(header, value)
            self.end_headers()
            # HEAD carries the GET headers, Content-Length included, no body.
            if self.command != "HEAD":
                self.wfile.write(payload)
        except ConnectionError:
            # A caller that hung up before the answer was written leaves nothing
            # to answer, and left to propagate the failed write reaches
            # socketserver's default handle_error, which prints the peer's
            # address and a full traceback (CWE-209, CWE-532). A probe that
            # hangs up is routine, not an error. ConnectionError is exactly the
            # peer-disconnect family and nothing wider, so a defect in this
            # handler still propagates.
            return


def resolve_host(environ=None):
    """Returns the bind address from ``HOST``, or the loopback default.

    An unset, empty or whitespace-only value keeps the loopback default rather
    than exposing the listener on every interface or failing to bind at all.

    ``environ`` is a parameter so the decision is a pure function of a mapping:
    every documented form is then exercisable without a test writing into the
    process-wide ``os.environ``, which any concurrent thread would also see.
    """
    if environ is None:
        environ = os.environ
    configured = environ.get("HOST", "").strip()
    return configured or DEFAULT_HOST


def resolve_port(environ=None):
    """Returns the listen port from ``PORT``, or the default. Never raises.

    Every invalid form resolves to ``DEFAULT_PORT``, so a malformed value can
    never abort start-up or print a traceback, while ``0`` is honoured as a
    request for an ephemeral port.

    Only ASCII digits pass the first screen, so the three applications resolve
    the same value from the same string: ``int()`` alone would also accept
    ``"+1234"``, ``"1_234"`` and non-ASCII digits, which the siblings reject.
    Leading zeros are then dropped and the remainder bounded *before* the
    conversion, which keeps ``int()`` away from an oversized digit run while
    still resolving ``"000080"`` to 80.

    ``environ`` is a parameter for the reason given on :func:`resolve_host`.
    """
    if environ is None:
        environ = os.environ
    configured = environ.get("PORT", "").strip()
    if not configured or not all(
        character in "0123456789" for character in configured
    ):
        return DEFAULT_PORT
    digits = configured.lstrip("0")
    if len(digits) > MAX_PORT_DIGITS:
        return DEFAULT_PORT
    # An all-zero value strips to the empty string and names port 0.
    port = int(digits) if digits else 0
    if port > MAX_PORT:
        return DEFAULT_PORT
    return port


# Tested with ``is None`` rather than for truthiness, so an explicit ``port=0``
# reaches the socket and yields an ephemeral port.
def create_server(host=None, port=None):
    if host is None:
        host = resolve_host()
    if port is None:
        port = resolve_port()
    return ThreadingHTTPServer((host, port), HealthRequestHandler)


def serve():
    try:
        server = create_server()
    except OSError:
        # A bind failure would otherwise print a traceback carrying absolute
        # paths and library internals, so it is reported as one fixed sentence
        # naming the variables to check, never their values.
        print(
            f"{APP_NAME} {APP_VERSION} could not bind the health endpoint;"
            " check HOST and PORT",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}{HEALTH_PATH}"
    # stdout is block-buffered when redirected, so without flush the banner
    # would sit in the buffer while the endpoint was already answering.
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
