import contextlib
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from unittest import mock

import app


# The contract's timestamp grammar: RFC 3339, UTC, second precision, Z-suffixed.
# No milliseconds, no microseconds and no ``+00:00`` offset are permitted.
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# The name the endpoint must report; it is this repository's own name.
EXPECTED_NAME = "child_repo_10_LOC"

# The version the endpoint must report; the composition seeds every level here.
EXPECTED_VERSION = "1.0.0"

# The healthy status value, spelled as the IETF health-check draft accepts it.
EXPECTED_STATUS = "UP"

# The document's keys, in the order the wire contract fixes them. ``json.dumps``
# follows insertion order, so that order reaches the wire and is assertable.
EXPECTED_KEYS = ["name", "version", "timestamp", "status"]

# The number of fields the document may carry. Nothing else belongs in a health
# answer: no hostname, process id, uptime, dependency detail, environment value
# or stack trace.
EXPECTED_FIELD_COUNT = 4

# The only media type the endpoint may serve -- never ``text/html``.
EXPECTED_MEDIA_TYPE = "application/json"

# A liveness answer carries a per-request timestamp, so it must never be cached.
EXPECTED_CACHE_CONTROL = "no-store"

# The ``Allow`` header of a 405 response; the ``", "`` spacing is part of the
# contract, because all three implementations emit the identical string.
EXPECTED_ALLOW = "GET, HEAD"

# What the ``Server`` header must name. The handler sets ``sys_version = ""`` so
# the interpreter version is never advertised, and the base class still joins the
# two halves with a space, so the value carries one trailing space that is
# stripped before comparison rather than asserted against.
EXPECTED_SERVER = "child_repo_10_LOC/1.0.0"

# The fixed error bodies. Neither may ever grow to include the path that was
# asked for or the method that was used.
NOT_FOUND_BODY = b'{"error":"Not Found"}'
METHOD_NOT_ALLOWED_BODY = b'{"error":"Method Not Allowed"}'

# Methods the endpoint must reject, each paired with the body urllib should send.
# POST carries an empty body so urllib really issues a POST rather than falling
# back to GET. ``FOO`` is invented on purpose: an unrecognised verb is the only
# way to prove that nothing falls through to the stock 501 HTML page.
REJECTED_METHODS = (
    ("POST", b""),
    ("OPTIONS", None),
    ("DELETE", None),
    ("FOO", None),
)

# Every request is bounded, so a hung endpoint fails a test instead of stalling
# the whole run. The same bound is reused for the teardown join.
REQUEST_TIMEOUT = 5

# A per-request timestamp only shows itself once a second boundary is crossed,
# so the check polls up to this deadline rather than sleeping a fixed second.
FRESHNESS_TIMEOUT = 5.0
FRESHNESS_POLL_INTERVAL = 0.1

# What ``python3 app.py`` must write, byte for byte, when it is given no
# arguments: the greeting and a single newline from ``print``, and nothing else
# on either stream. Compared as bytes rather than as text so that a stray
# carriage return or a second line fails the comparison instead of being
# normalised away by universal-newline decoding.
DEFAULT_RUN_STDOUT = b"Hello Lakshya\n"

# The module's own path, and the directory to run it from. Taken from the
# imported module rather than from ``__file__`` so the assertion follows the
# module under test wherever the suite is invoked from.
APP_PATH = os.path.abspath(app.__file__)
APP_DIRECTORY = os.path.dirname(APP_PATH)

# One abort reproduces the disclosure this guards against; a handful proves it
# without lengthening the run.
ABORT_ATTEMPTS = 8

# The address the server under test is bound to. Port 0 is deliberate: the
# operating system assigns an ephemeral port, and the port actually assigned is
# read back from the bound server rather than assumed.
LOOPBACK_HOST = "127.0.0.1"
EPHEMERAL_PORT = 0

# A ``PORT`` of nothing but digits that no port could ever need. It is the one
# malformed form that reaches ``int()`` looking legitimate, because a digit run
# beyond the interpreter's integer-conversion limit raises instead of parsing,
# so the resolver has to reject it by length before converting it.
OVERLONG_NUMERIC_PORT = "9" * 4301

# Sends a TCP RST instead of a FIN when the socket is closed, which is how a
# client that gives up mid-exchange really looks: a linger timeout of zero
# discards whatever is still queued. A FIN alone would let the server finish
# writing into a half-closed connection and prove nothing.
ABORTING_LINGER = struct.pack("ii", 1, 0)

# The peer-disconnect exceptions a response write can raise. All three are
# ``ConnectionError`` subclasses, and each must be absorbed, not reported.
PEER_DISCONNECT_ERRORS = (
    BrokenPipeError(32, "Broken pipe"),
    ConnectionResetError(104, "Connection reset by peer"),
    ConnectionAbortedError(103, "Software caused connection abort"),
)


class AbortedPeerWriter:
    """A ``wfile`` whose every write fails the way a reset peer's does.

    The failure is raised on the first write, which is the header flush,
    because that is where a real abort was measured to land.
    """

    def __init__(self, error):
        self.error = error
        self.writes = 0

    def write(self, data):
        self.writes += 1
        raise self.error

    def flush(self):
        pass


class DetachedHandler(app.HealthRequestHandler):
    """The handler's response path with a stub writer instead of a socket.

    ``BaseHTTPRequestHandler.__init__`` runs a whole request cycle against a
    real connection, so it is deliberately not called: only the attributes the
    response path reads are set. That makes the write failure happen exactly
    when the test says it does, rather than depending on whether a real peer's
    reset happens to win a race with the server's write.
    """

    def __init__(self, writer, command="GET"):
        self.wfile = writer
        self.command = command
        self.requestline = "%s /health HTTP/1.1" % command
        self.request_version = "HTTP/1.1"
        self.close_connection = False


def resolved_port(value):
    """Returns ``app.resolve_port()`` with ``PORT`` set to ``value``.

    ``None`` means the variable is absent altogether. The environment is
    restored when the context exits, so a case can never leak into the next one
    or into the live server the rest of this file exercises.
    """
    with mock.patch.dict(os.environ):
        if value is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = value
        return app.resolve_port()


def resolved_host(value):
    """Returns ``app.resolve_host()`` with ``HOST`` set to ``value``."""
    with mock.patch.dict(os.environ):
        if value is None:
            os.environ.pop("HOST", None)
        else:
            os.environ["HOST"] = value
        return app.resolve_host()


def header_value(headers, name):
    """Returns a response header value, or ``""`` when the field is absent.

    Field names are matched case-insensitively, which is what ``email.message``
    already does for us: RFC 9110 makes HTTP field names case-insensitive, and
    the sibling Java implementation legitimately spells the very same fields
    ``Content-type`` and ``Cache-control``. No header name is therefore ever
    compared as a string here. A missing field reads back as the empty string so
    that it fails an assertion with a readable diff instead of raising.
    """
    value = headers.get(name)
    if value is None:
        return ""
    return value


class ExistingBehaviourTest(unittest.TestCase):
    """The capability this repository had before the health endpoint existed."""

    def test_the_original_greeting_is_unchanged(self):
        # The module's only pre-feature behaviour, and the mechanical guarantee
        # that adding an endpoint preserved it.
        self.assertEqual("Hello Lakshya", app.greet("Lakshya"))
        # The default program prints greet("Lakshya"), so that exact call is the
        # one that matters; a second name proves the greeting is still built
        # from its argument rather than from a captured constant.
        self.assertEqual("Hello world", app.greet("world"))
        # Calling greet() proves the function. It cannot prove the program: the
        # ``if __name__ == "__main__":`` branch is what an operator actually
        # runs, and it is the branch the ``--serve`` gate exists to protect,
        # because a process that binds a socket never exits and would have
        # replaced this output rather than added to it. So the program is run,
        # in a real child process, exactly as it is documented to be run.
        completed = subprocess.run(
            # -B keeps the interpreter from writing a __pycache__ directory
            # beside the sources: this repository's working tree is expected to
            # stay clean, and a test must not be what dirties it.
            [sys.executable, "-B", APP_PATH],
            cwd=APP_DIRECTORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=REQUEST_TIMEOUT,
        )
        # Exit status, standard output and standard error are all part of the
        # preserved behaviour, so all three are asserted: the greeting and its
        # single newline on stdout, nothing at all on stderr -- a banner, a
        # warning or a traceback there would be a change even with the right
        # stdout -- and a clean exit.
        self.assertEqual(DEFAULT_RUN_STDOUT, completed.stdout)
        self.assertEqual(b"", completed.stderr)
        self.assertEqual(0, completed.returncode)
        # The listener must not start without the flag. Had it started, the run
        # above would have blocked until the timeout instead of returning, so
        # reaching this line already proves it; the byte count is asserted too,
        # because a startup banner is the one thing that could be added without
        # changing the first line.
        self.assertEqual(len(DEFAULT_RUN_STDOUT), len(completed.stdout))


class PayloadAndConfigurationTest(unittest.TestCase):
    """Everything the module answers without involving HTTP at all.

    That is the health document itself, and the two environment variables that
    decide where its listener is placed. Both resolvers are pure functions of
    the environment, which is what lets every malformed form be exercised here
    rather than by starting a server on the default port and competing with
    whatever else is on it.
    """

    def test_the_health_payload_reports_the_four_contract_fields_in_order(self):
        payload = app.health_payload()
        self.assertEqual(EXPECTED_KEYS, list(payload.keys()))
        # Counting the keys is the only way to prove nothing extra leaked into
        # the answer, so the count is asserted rather than inferred.
        self.assertEqual(EXPECTED_FIELD_COUNT, len(payload))
        self.assertEqual(EXPECTED_NAME, payload["name"])
        self.assertEqual(app.APP_NAME, payload["name"])
        self.assertEqual(EXPECTED_VERSION, payload["version"])
        self.assertEqual(app.APP_VERSION, payload["version"])
        self.assertEqual(EXPECTED_STATUS, payload["status"])
        for key in EXPECTED_KEYS:
            # All four values are JSON strings, including the version, which a
            # reader could otherwise be tempted to serve as a number.
            self.assertIsInstance(payload[key], str, "field %s" % key)
        # A fresh document per call: the builder must not hand out one shared
        # module-level dict that a single caller could mutate for every later
        # caller, and a per-request timestamp is impossible if it did.
        payload["status"] = "DOWN"
        self.assertEqual(EXPECTED_STATUS, app.health_payload()["status"])
        # The timestamp is one of the four fields, so the grammar it must be
        # written in is asserted here too -- on the document and on the builder
        # behind it: a second-precision UTC instant, Z-suffixed, with no
        # milliseconds and no offset.
        self.assertRegex(app.current_timestamp(), TIMESTAMP_PATTERN)
        self.assertRegex(app.health_payload()["timestamp"], TIMESTAMP_PATTERN)

    def test_a_malformed_port_or_host_falls_back_instead_of_raising(self):
        # The value on the left, the port the endpoint must bind on the right.
        # An operator who mistypes PORT gets a running endpoint on the
        # documented default, never a traceback: the fallback is the whole
        # contract here.
        for configured, expected in (
            (None, app.DEFAULT_PORT),       # unset
            ("", app.DEFAULT_PORT),         # blank
            ("   ", app.DEFAULT_PORT),      # whitespace only
            ("abc", app.DEFAULT_PORT),      # not a number
            ("8000abc", app.DEFAULT_PORT),  # trailing rubbish, not a prefix
            ("+8000", app.DEFAULT_PORT),    # signed; the siblings agree
            ("-1", app.DEFAULT_PORT),       # negative
            ("80_00", app.DEFAULT_PORT),    # int() takes it; the wire must not
            ("8.5", app.DEFAULT_PORT),      # not an integer
            ("65536", app.DEFAULT_PORT),    # one past the last port
            ("99999", app.DEFAULT_PORT),    # five digits, out-of-range value
            # The regression this test exists for: all digits, far too many.
            (OVERLONG_NUMERIC_PORT, app.DEFAULT_PORT),
            ("8000", 8000),                 # the documented default itself
            (" 8080 ", 8080),               # padded by a shell, trimmed here
            ("0", 0),                       # an explicit ephemeral port
            ("000080", 80),                 # zero-padded, as siblings read it
            ("00000", 0),                   # all zeros still name port 0
        ):
            label = configured if configured is None else configured[:16]
            with self.subTest(port=label):
                self.assertEqual(expected, resolved_port(configured))
        # The same fallback read from the other variable, and asserted in the
        # same test because it is the same behaviour. Loopback unless an
        # operator opts in: an unset or blank HOST must never be read as "every
        # interface", which is the one mistake here that would put the endpoint
        # on the network.
        self.assertEqual(app.DEFAULT_HOST, resolved_host(None))
        self.assertEqual(app.DEFAULT_HOST, resolved_host(""))
        self.assertEqual(app.DEFAULT_HOST, resolved_host("   "))
        self.assertEqual("127.0.0.1", app.DEFAULT_HOST)
        # A named address is honoured, and a padded one is trimmed to the
        # address it names rather than handed to the socket with its spaces.
        self.assertEqual("127.0.0.2", resolved_host("127.0.0.2"))
        self.assertEqual("127.0.0.2", resolved_host("  127.0.0.2  "))


class HealthEndpointContractTest(unittest.TestCase):
    """The live endpoint, exercised over HTTP against a really bound socket."""

    @classmethod
    def setUpClass(cls):
        # Explicit arguments, never the environment, so a run cannot depend on
        # the shell that started it. The class binds once for all three of its
        # tests.
        try:
            server = app.create_server(LOOPBACK_HOST, EPHEMERAL_PORT)
        except OSError as error:
            raise AssertionError(
                "the suite could not bind an ephemeral loopback port: %s"
                % (error,)
            ) from error
        try:
            host, port = server.server_address[:2]
            # Only what ephemeral binding actually guarantees is asserted here:
            # the address is loopback and a port was assigned. Which number the
            # operating system hands out is not ours to constrain.
            if host != LOOPBACK_HOST or port <= 0:
                raise AssertionError(
                    "the suite bound %r:%r instead of an ephemeral loopback "
                    "port" % (host, port)
                )
            cls._assert_an_explicit_port_reaches_the_socket()
        except BaseException:
            # A failure in setUpClass skips tearDownClass, so the listener
            # bound above is released here rather than outliving the run.
            server.server_close()
            raise
        cls.server = server
        cls.base_url = "http://%s:%d" % (host, port)
        # A daemon thread cannot keep the interpreter alive if a test raises
        # before teardown runs, so a failure can never hang the run.
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            name="health-endpoint-under-test",
            daemon=True,
        )
        cls.server_thread.start()

    @staticmethod
    def _assert_an_explicit_port_reaches_the_socket():
        """Checks that an explicit port is passed through to ``create_server``.

        ``create_server`` tests its arguments with ``is None`` precisely so an
        explicit 0 still reaches the socket. Were a falsy port ever read as
        "unset", this suite would silently fall back to the fixed default port
        and compete for it with a developer's own running server, so a port
        the operating system has just released is asked for by name and the
        bound port is compared against it.
        """
        try:
            reserved = app.create_server(LOOPBACK_HOST, EPHEMERAL_PORT)
            try:
                requested = reserved.server_address[1]
            finally:
                reserved.server_close()
            pinned = app.create_server(LOOPBACK_HOST, requested)
            try:
                bound = pinned.server_address[1]
            finally:
                pinned.server_close()
        except OSError as error:
            raise AssertionError(
                "the explicit-port check could not bind a loopback port: %s"
                % error
            ) from error
        if bound != requested:
            raise AssertionError(
                "an explicit port did not reach the socket: %r was asked for "
                "and %r was bound" % (requested, bound)
            )

    @classmethod
    def tearDownClass(cls):
        # All three steps, in this order: shutdown() stops the serve_forever
        # loop, server_close() releases the listening socket and joins the
        # handler threads, and the join below proves the serving thread really
        # ended. Nothing -- listener, thread or socket -- may outlive the run.
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=REQUEST_TIMEOUT)
        if cls.server_thread.is_alive():
            raise AssertionError(
                "the serving thread outlived shutdown(); a listener would "
                "survive the test run"
            )

    def _send(self, path, method=None, data=None):
        """Sends one request and returns the open response; the caller closes it.

        ``method`` is left unset for a plain GET. Every call is bounded by
        ``REQUEST_TIMEOUT`` so a hung endpoint fails a test rather than stalling
        the suite, and a fresh connection is used for each request because every
        response from this endpoint closes its own connection.
        """
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method
        )
        return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)

    def _read(self, path, method=None, data=None):
        """Returns ``(status, headers, body)`` for a request that must succeed."""
        response = self._send(path, method=method, data=data)
        try:
            return response.status, response.headers, response.read()
        finally:
            response.close()

    def _read_error(self, path, method=None, data=None):
        """Returns ``(code, headers, body)`` for a request that must be refused.

        urllib raises ``HTTPError`` for a 4xx, and that exception *is* the
        response, so the status code, the header set and the body are all read
        back from it. Failing to raise is itself a failed assertion.
        """
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._send(path, method=method, data=data)
        error = caught.exception
        try:
            return error.code, error.headers, error.read()
        finally:
            error.close()

    def _raw_exchange(self, request_line):
        """Speaks one HTTP/1.1 exchange over a socket and splits the reply.

        Returns ``(header_block, body)`` with the two separated at the first
        CRLF pair, so the caller can count the bytes that genuinely followed the
        header terminator. ``Connection: close`` makes the server end the
        stream, which is what lets the whole reply be read to EOF without
        interpreting ``Content-Length`` first -- the very header under test.
        """
        host, port = self.server.server_address[:2]
        connection = socket.create_connection(
            (host, port), timeout=REQUEST_TIMEOUT
        )
        try:
            connection.sendall(
                request_line
                + b" HTTP/1.1\r\nHost: "
                + ("%s:%d" % (host, port)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n"
            )
            received = b""
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received += chunk
        finally:
            connection.close()
        header_block, separator, body = received.partition(b"\r\n\r\n")
        self.assertEqual(
            b"\r\n\r\n", separator, "the response head was never terminated"
        )
        return header_block, body

    def _assert_the_common_headers(self, headers, body, context):
        """Asserts the headers a refusal owes just as much as a success does.

        A 404 or a 405 is still an answer from this endpoint, so it carries the
        same media type, the same cache directive and a byte-accurate length --
        and never the stock ``text/html`` error page.
        """
        content_type = header_value(headers, "Content-Type")
        self.assertEqual(EXPECTED_MEDIA_TYPE, content_type, context)
        self.assertNotIn("text/html", content_type, context)
        self.assertEqual(
            EXPECTED_CACHE_CONTROL,
            header_value(headers, "Cache-Control"),
            context,
        )
        announced_length = header_value(headers, "Content-Length")
        self.assertNotEqual(
            "", announced_length, "%s: Content-Length is mandatory" % context
        )
        self.assertEqual(len(body), int(announced_length), context)

    def _await_a_later_timestamp(self, first):
        """Polls ``/health`` until the reported second changes.

        Grammar alone cannot tell a per-request timestamp from one captured
        once at import time, so the answer has to be seen changing. The wait is
        bounded and every polled answer is checked, so a stuck timestamp fails
        the test instead of hanging the run.
        """
        deadline = time.monotonic() + FRESHNESS_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(FRESHNESS_POLL_INTERVAL)
            _, _, raw = self._read("/health")
            latest = json.loads(raw)["timestamp"]
            self.assertRegex(latest, TIMESTAMP_PATTERN)
            if latest != first:
                return latest
        self.fail(
            "GET /health kept reporting %r for %s seconds, so the timestamp "
            "is not generated per request" % (first, FRESHNESS_TIMEOUT)
        )

    def test_get_health_responds_two_hundred_with_the_contract_envelope(self):
        status, headers, raw = self._read("/health")
        self.assertEqual(200, status)
        self.assertEqual(
            EXPECTED_MEDIA_TYPE, header_value(headers, "Content-Type")
        )
        self.assertEqual(
            EXPECTED_CACHE_CONTROL, header_value(headers, "Cache-Control")
        )
        # Content-Length is a byte count, so it is compared against the bytes
        # that actually arrived rather than against a character count.
        announced_length = header_value(headers, "Content-Length")
        self.assertNotEqual("", announced_length, "Content-Length is mandatory")
        self.assertEqual(len(raw), int(announced_length))
        # Re-serialising the parsed document with compact separators reproduces
        # the exact bytes only when the wire form was itself compact and its keys
        # were in the contract's order, so this one comparison proves both.
        self.assertEqual(
            json.dumps(json.loads(raw), separators=(",", ":")).encode("utf-8"),
            raw,
        )
        payload = json.loads(raw)
        self.assertEqual(EXPECTED_KEYS, list(payload.keys()))
        self.assertEqual(EXPECTED_FIELD_COUNT, len(payload))
        self.assertEqual(EXPECTED_NAME, payload["name"])
        self.assertEqual(EXPECTED_VERSION, payload["version"])
        self.assertEqual(EXPECTED_STATUS, payload["status"])
        self.assertRegex(payload["timestamp"], TIMESTAMP_PATTERN)
        # The handler suppresses the interpreter version banner, so the Server
        # header names the application and discloses nothing about the runtime
        # it happens to be built on.
        server = header_value(headers, "Server")
        self.assertNotIn("python", server.lower())
        self.assertTrue(
            server.startswith(EXPECTED_SERVER), "Server header was [%s]" % server
        )
        self.assertEqual(EXPECTED_SERVER, server.strip())
        # A load balancer is likely to probe with a query string, which must not
        # defeat the path match.
        probed_status, _, probed_raw = self._read("/health?probe=lb")
        self.assertEqual(200, probed_status)
        self.assertEqual(EXPECTED_KEYS, list(json.loads(probed_raw).keys()))
        # The timestamp is generated per request, not once at import time, so a
        # later answer must report a later second.
        later = self._await_a_later_timestamp(payload["timestamp"])
        self.assertRegex(later, TIMESTAMP_PATTERN)
        self.assertNotEqual(payload["timestamp"], later)

    def test_head_health_returns_the_get_headers_without_a_body(self):
        # The length is compared against a real GET rather than a literal, so
        # the assertion stays true whatever second the timestamp names.
        get_status, get_headers, get_body = self._read("/health")
        self.assertEqual(200, get_status)
        head_status, head_headers, head_body = self._read("/health", method="HEAD")
        # RFC 9110 expects a general-purpose server to answer HEAD wherever it
        # answers GET, so a 405 here would be a defect rather than a nicety.
        self.assertEqual(200, head_status)
        self.assertEqual(
            EXPECTED_MEDIA_TYPE, header_value(head_headers, "Content-Type")
        )
        self.assertEqual(
            header_value(get_headers, "Content-Type"),
            header_value(head_headers, "Content-Type"),
        )
        self.assertEqual(
            EXPECTED_CACHE_CONTROL, header_value(head_headers, "Cache-Control")
        )
        self.assertEqual(
            header_value(get_headers, "Cache-Control"),
            header_value(head_headers, "Cache-Control"),
        )
        # The length a GET would have returned is announced even though nothing
        # is written: the header set is the GET's, only the body is absent.
        announced_length = header_value(head_headers, "Content-Length")
        self.assertNotEqual("", announced_length, "Content-Length is mandatory")
        self.assertGreater(int(announced_length), 0)
        self.assertEqual(len(get_body), int(announced_length))
        self.assertEqual(b"", head_body)
        # The assertion above cannot stand on its own: http.client knows a HEAD
        # response has no body and stops reading at the header terminator, so a
        # server that wrongly wrote one would still read back as empty through
        # urllib. The exchange below therefore speaks HTTP straight over the
        # socket and counts the bytes that really followed the terminator.
        header_block, wire_body = self._raw_exchange(b"HEAD /health")
        self.assertTrue(
            header_block.startswith(b"HTTP/1.1 200"),
            "status line was [%r]" % (header_block.split(b"\r\n", 1)[0],),
        )
        # Lower-cased on both sides, because the field name's casing is not part
        # of the contract even when it is read as raw bytes.
        self.assertIn(
            b"content-length: %d" % len(get_body), header_block.lower()
        )
        self.assertEqual(b"", wire_body)

    def _abort(self, request=None):
        """Opens a connection, optionally sends ``request``, then resets it.

        ``SO_LINGER`` with a zero timeout is what turns the close into a TCP
        RST rather than an orderly shutdown, so the server's next read or write
        on that connection really fails. With ``request`` given, the abort
        lands while the response is being written; without it, while the
        request is still being read -- the two paths a giving-up probe takes.
        """
        connection = socket.create_connection(
            self.server.server_address[:2], timeout=REQUEST_TIMEOUT
        )
        try:
            if request is not None:
                connection.sendall(request)
            connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, ABORTING_LINGER
            )
        finally:
            connection.close()

    def _wait_for_handlers(self, baseline_threads):
        """Blocks until every handler thread the server started has ended."""
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while (
            threading.active_count() > baseline_threads
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

    def test_aborted_callers_and_refused_requests_disclose_nothing(self):
        """Every way this endpoint answers a request it will not serve.

        A caller that gives up mid-exchange, a path that is not the route and
        a method the route does not allow are one property seen from three
        angles: whatever this endpoint cannot answer, what it says back names
        neither the request, nor the caller, nor the machine that answered. The
        three parts below are, in order, the aborting caller over a real
        socket, the fixed ``404`` and ``405`` envelopes, and the response
        writer's own behaviour once the socket underneath it has gone.
        """
        # Part 1 -- a caller that hangs up mid-exchange. A probe that gives up
        # is routine, but the write it interrupts raises, and an exception
        # escaping the handler reaches socketserver's default handle_error,
        # which prints the peer's address and a full traceback to standard
        # error. Suppressing the access log does not cover that path.
        host, port = self.server.server_address[:2]
        request = (
            b"GET /health HTTP/1.1\r\nHost: "
            + ("%s:%d" % (host, port)).encode("ascii")
            + b"\r\n\r\n"
        )
        baseline_threads = threading.active_count()
        captured = io.StringIO()
        # Standard error is captured rather than inspected afterwards because
        # the disclosure under test is written there by socketserver's own
        # handle_error: the peer's address on one line and a full traceback --
        # absolute module paths included -- on the next.
        with contextlib.redirect_stderr(captured):
            for _ in range(ABORT_ATTEMPTS):
                self._abort(request)
            for _ in range(ABORT_ATTEMPTS):
                self._abort()
            # A normal request after the aborts proves the endpoint survived
            # them, and because connections are accepted in order it also
            # guarantees every aborted connection has already reached a handler
            # thread by the time the wait below starts.
            status, _, raw = self._read("/health")
            self._wait_for_handlers(baseline_threads)
        self.assertEqual(
            "",
            captured.getvalue(),
            "an aborting client made the server write to standard error",
        )
        self.assertEqual(200, status)
        self.assertEqual(EXPECTED_KEYS, list(json.loads(raw).keys()))
        # Part 2 -- an unknown path, then every unsupported method: the two
        # fixed JSON envelopes, asserted byte-for-byte because their whole
        # purpose is to carry nothing that came in with the request.
        code, headers, body = self._read_error("/nope")
        self.assertEqual(404, code)
        self._assert_the_common_headers(headers, body, "path /nope")
        self.assertEqual(NOT_FOUND_BODY, body)
        # An inbound request is untrusted input, and this endpoint is the first
        # the system has ever accepted, so the answer must not quote the
        # requested path back at the caller.
        self.assertNotIn(b"nope", body)
        for method, data in REJECTED_METHODS:
            # OPTIONS and the invented verb FOO are the assertions that matter
            # most in this file: without the send_error override in app.py,
            # http.server answers an unsupported verb with 501 and a stock
            # text/html page whose body repeats the caller's own method back at
            # them -- a contract break and an information disclosure at once.
            context = "method %s" % method
            code, headers, body = self._read_error(
                "/health", method=method, data=data
            )
            self.assertEqual(405, code, context)
            self.assertEqual(
                EXPECTED_ALLOW, header_value(headers, "Allow"), context
            )
            self._assert_the_common_headers(headers, body, context)
            self.assertEqual(METHOD_NOT_ALLOWED_BODY, body, context)
            self.assertNotIn(method.encode("ascii"), body, context)
        # Part 3 -- the response writer with its socket replaced by a double,
        # which is what makes the write fail exactly where this test says it
        # does instead of depending on whether a real peer's reset wins a race
        # with the server's write. Each peer-disconnect exception the write can
        # raise is exercised in turn.
        for error in PEER_DISCONNECT_ERRORS:
            with self.subTest(error=type(error).__name__):
                writer = AbortedPeerWriter(error)
                handler = DetachedHandler(writer)
                # No assertRaises: the point is that nothing comes back out.
                # Were this to raise, the traceback would carry the peer
                # address into the operator's log by way of handle_error.
                handler._send_json(HTTPStatus.OK, app.health_payload())
                # The write really was attempted, so the test is exercising the
                # failure path rather than passing because nothing happened.
                self.assertGreaterEqual(writer.writes, 1)
                # The connection is marked closed, so the request loop ends
                # instead of trying to read another request from a dead socket.
                self.assertTrue(handler.close_connection)
        # The guard has to stay narrow. A failure that is not a peer disconnect
        # means something is wrong in this application, and hiding it would
        # turn every future defect on the response path into a silent reply.
        writer = AbortedPeerWriter(ValueError("not a peer disconnect"))
        handler = DetachedHandler(writer)
        with self.assertRaises(ValueError):
            handler._send_json(HTTPStatus.OK, app.health_payload())


if __name__ == "__main__":
    unittest.main()
