"""Standard-library test suite for the ``/health`` endpoint of :mod:`app`.

Run it from this directory as ``python3 -B -m unittest``; the README explains
why bytecode writing is suppressed rather than left to its default.

Every server this suite binds -- the one it hosts in-process and the one it
starts through ``app.py --serve`` -- takes port 0, so a run never competes with
a developer's own ``--serve`` or with the sibling suites. Nothing here mutates
``os.environ``, reads or writes a file, or reaches outside this repository.
"""

import contextlib
import io
import json
import os
import re
import signal
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

import app


TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

EXPECTED_NAME = "child_repo_10_LOC"

EXPECTED_VERSION = "1.0.0"

EXPECTED_STATUS = "UP"

# ``json.dumps`` follows insertion order, so the contract's key order reaches
# the wire and is assertable.
EXPECTED_KEYS = ["name", "version", "timestamp", "status"]

EXPECTED_FIELD_COUNT = 4

EXPECTED_MEDIA_TYPE = "application/json"

EXPECTED_CACHE_CONTROL = "no-store"

EXPECTED_ALLOW = "GET, HEAD"

# The base class composes the field as ``server_version + " " + sys_version``,
# and the handler empties the second half, so what arrives is this value plus
# the separator left behind -- which RFC 9110 lets a recipient strip. Asserted
# stripped, and again against the bytes on the wire.
EXPECTED_SERVER = "child_repo_10_LOC/1.0.0"

NOT_FOUND_BODY = b'{"error":"Not Found"}'
METHOD_NOT_ALLOWED_BODY = b'{"error":"Method Not Allowed"}'
BAD_REQUEST_BODY = b'{"error":"Bad Request"}'

# Targets only a raw socket can send -- urllib rewrites or refuses every one --
# each a spelling some part of the stack could be tempted to read as the route.
# The contract compares the target exactly as it arrived, so all are 404.
# ``POST ///health`` proves the target is judged before the method.
RAW_FOREIGN_REQUEST_LINES = (
    b"GET ///health",
    b"GET //health",
    b"GET //health/x",
    b"GET /a/../health",
    b"GET http://127.0.0.1/health",
    b"POST ///health",
)

# Missing the ``Host`` field RFC 9112 requires of an HTTP/1.1 message. All four
# owe the one fixed 400: the field is checked before target or method.
MISSING_HOST_REQUEST_LINES = (
    b"GET /health",
    b"HEAD /health",
    b"GET /nope",
    b"FOO /health",
)

BANNER_PATTERN = re.compile(
    r"^child_repo_10_LOC 1\.0\.0 listening on "
    r"http://127\.0\.0\.1:(\d+)/health$"
)

# POST carries an empty body so urllib really issues a POST rather than falling
# back to GET. ``FOO`` is invented on purpose: an unrecognised verb is the only
# way to prove nothing falls through to the stock 501 HTML page.
REJECTED_METHODS = (
    ("POST", b""),
    ("OPTIONS", None),
    ("DELETE", None),
    ("FOO", None),
)

REQUEST_TIMEOUT = 5

# A per-request timestamp only shows itself once a second boundary is crossed,
# so the check polls to this deadline rather than sleeping a fixed second.
FRESHNESS_TIMEOUT = 5.0
FRESHNESS_POLL_INTERVAL = 0.1

# Compared as bytes rather than as text, so a stray carriage return fails the
# comparison instead of being normalised away by universal-newline decoding.
DEFAULT_RUN_STDOUT = b"Hello Lakshya\n"

# Taken from the imported module rather than from ``__file__``, so a subprocess
# runs the module under test wherever the suite was invoked from.
APP_PATH = os.path.abspath(app.__file__)
APP_DIRECTORY = os.path.dirname(APP_PATH)

ABORT_ATTEMPTS = 8

# Port 0 is deliberate: the operating system assigns the port, and the number it
# assigned is read back from the bound server rather than assumed.
LOOPBACK_HOST = "127.0.0.1"
EPHEMERAL_PORT = 0

# The one malformed form that reaches ``int()`` looking legitimate: a digit run
# this long raises instead of parsing, so it must be rejected by length first.
OVERLONG_NUMERIC_PORT = "9" * 4301

# A zero linger timeout closes with a TCP RST instead of a FIN, which is how a
# client that gives up really looks; a FIN alone would let the server finish
# writing into a half-closed connection and prove nothing.
ABORTING_LINGER = struct.pack("ii", 1, 0)

# Each of these a response write can raise, and each must be absorbed rather
# than reported.
PEER_DISCONNECT_ERRORS = (
    BrokenPipeError(32, "Broken pipe"),
    ConnectionResetError(104, "Connection reset by peer"),
    ConnectionAbortedError(103, "Software caused connection abort"),
)


class AbortedPeerWriter:
    """A ``wfile`` whose every write fails the way a reset peer's does.

    Raised on the first write -- the header flush -- because that is where a
    real abort was measured to land.
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
    response path reads are set. The write then fails exactly where the test
    says it does, rather than racing a real peer's reset.
    """

    def __init__(self, writer, command="GET"):
        self.wfile = writer
        self.command = command
        self.requestline = "%s /health HTTP/1.1" % command
        self.request_version = "HTTP/1.1"
        self.close_connection = False


def environment(name, value):
    """Returns a mapping naming one variable, or an empty one for ``None``.

    The resolvers read the mapping they are given, so a case is a literal
    dictionary rather than a write into ``os.environ`` -- which is
    process-wide, and would be seen by the server thread this file runs and by
    every subprocess started afterwards.
    """
    return {} if value is None else {name: value}


def resolved_port(value):
    return app.resolve_port(environment("PORT", value))


def resolved_host(value):
    return app.resolve_host(environment("HOST", value))


def header_value(headers, name):
    """Returns a response header value, or ``""`` when the field is absent.

    Matched case-insensitively, as ``email.message`` already does and RFC 9110
    requires: the sibling Java implementation legitimately spells the same
    fields ``Content-type`` and ``Cache-control``. A missing field reads back
    empty so it fails with a readable diff instead of raising.
    """
    value = headers.get(name)
    if value is None:
        return ""
    return value


def raw_request(port, request_line, with_host=True):
    """Speaks one HTTP/1.1 exchange over a socket and splits the reply.

    Returns ``(header_block, separator, body)`` split at the first CRLF pair,
    so a caller can prove the head was terminated and count the bytes that
    genuinely followed. Reading to EOF is complete because every response from
    this endpoint closes its connection, so ``Content-Length`` -- often the
    header under test -- never has to be trusted to find the end.

    A socket rather than urllib because several forms this contract answers
    cannot be expressed through a client: urllib supplies a ``Host`` field of
    its own, making its absence unaskable, and rewrites a target such as
    ``//health`` before it reaches the wire.
    """
    host_field = b""
    if with_host:
        authority = ("%s:%d" % (LOOPBACK_HOST, port)).encode("ascii")
        host_field = b"Host: " + authority + b"\r\n"
    connection = socket.create_connection(
        (LOOPBACK_HOST, port), timeout=REQUEST_TIMEOUT
    )
    try:
        connection.sendall(
            request_line
            + b" HTTP/1.1\r\n"
            + host_field
            + b"Connection: close\r\n\r\n"
        )
        received = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            received += chunk
    finally:
        connection.close()
    return received.partition(b"\r\n\r\n")


class ExistingBehaviourTest(unittest.TestCase):
    """The greeting, and the ``--serve`` gate that keeps it reachable.

    Both branches of the gate are exercised, each in a real child process:
    without the flag the program greets and exits, with it the program serves.
    """

    def test_the_greeting_is_unchanged_and_only_serve_starts_a_listener(self):
        self.assertEqual("Hello Lakshya", app.greet("Lakshya"))
        self.assertEqual("Hello world", app.greet("world"))
        completed = self._run(APP_PATH)
        self.assertEqual(DEFAULT_RUN_STDOUT, completed.stdout)
        self.assertEqual(b"", completed.stderr)
        self.assertEqual(0, completed.returncode)
        self.assertEqual(len(DEFAULT_RUN_STDOUT), len(completed.stdout))
        # This file's own ``import app`` cannot show that importing is silent:
        # by the time a test runs the module is already in ``sys.modules`` and
        # anything it printed reached the runner's streams before collection.
        # So a fresh interpreter imports it and nothing else.
        imported = self._run("-c", "import app")
        self.assertEqual(b"", imported.stdout)
        self.assertEqual(b"", imported.stderr)
        self.assertEqual(0, imported.returncode)
        self._assert_serve_binds_serves_and_releases()

    def _assert_serve_binds_serves_and_releases(self):
        """Starts ``app.py --serve``, proves it serves, then stops it.

        ``PORT=0`` keeps the child off the documented default, where it would
        compete with a developer's own server, and the environment it is given
        is a copy: this suite never writes into ``os.environ``.

        The port the child announces is the port it is probed on, so a process
        that printed a banner and died, or bound something other than what it
        announced, fails here rather than passing.
        """
        environ = dict(os.environ, PORT="0", PYTHONDONTWRITEBYTECODE="1")
        environ.pop("HOST", None)
        # ``with`` closes the pipes and reaps the child however this method
        # leaves; the inner ``finally`` kills a child that is still running
        # first, because the context manager's own wait is unbounded.
        with subprocess.Popen(
            [sys.executable, "-B", APP_PATH, "--serve"],
            cwd=APP_DIRECTORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environ,
        ) as child:
            remaining_stdout, errors = self._exercise_and_stop(child)
        self.assertEqual(0, child.returncode)
        self.assertEqual(b"", errors)
        self.assertEqual(b"", remaining_stdout)

    def _exercise_and_stop(self, child):
        try:
            banner = self._first_line(child)
            matched = BANNER_PATTERN.match(banner)
            self.assertIsNotNone(
                matched, "the startup banner was %r" % (banner,)
            )
            port = int(matched.group(1))
            # An assigned port is never the documented default, which proves
            # PORT=0 reached the socket rather than being read as "unset".
            self.assertGreater(port, 0)
            self.assertNotEqual(app.DEFAULT_PORT, port)
            self.assertTrue(
                self._port_accepts(port),
                "the child announced port %d but nothing accepted a connection "
                "there" % port,
            )
            self._assert_the_child_serves_the_contract(port)
            self.assertIsNone(child.poll(), "the child exited while serving")
            # SIGINT is what Ctrl-C sends, and it is caught, so the loop ends
            # and the process exits cleanly. An uncaught KeyboardInterrupt
            # would exit non-zero with a traceback on stderr instead.
            child.send_signal(signal.SIGINT)
            streams = child.communicate(timeout=REQUEST_TIMEOUT)
        finally:
            if child.poll() is None:
                child.kill()
        self.assertTrue(
            self._port_is_released(port),
            "port %d was still accepting connections after shutdown" % port,
        )
        return streams

    @staticmethod
    def _first_line(child):
        """Returns the child's first line of standard output, decoded.

        Read in a daemon thread so a child that neither prints nor exits fails
        on a bound instead of hanging the run: a daemon cannot keep the
        interpreter alive if it is still blocked when the suite ends.
        """
        captured = []

        def read():
            try:
                captured.append(child.stdout.readline())
            except (OSError, ValueError):
                captured.append(b"")

        reader = threading.Thread(
            target=read, name="serve-banner-reader", daemon=True
        )
        reader.start()
        reader.join(timeout=REQUEST_TIMEOUT)
        if not captured:
            return ""
        return captured[0].decode("utf-8", "replace").rstrip("\r\n")

    def _assert_the_child_serves_the_contract(self, port):
        head, separator, body = raw_request(port, b"GET /health")
        self.assertEqual(b"\r\n\r\n", separator, "GET /health head")
        self.assertTrue(
            head.startswith(b"HTTP/1.1 200"),
            "GET /health status line was %r" % (head.split(b"\r\n", 1)[0],),
        )
        self.assertIn(b"content-type: application/json", head.lower())
        payload = json.loads(body)
        self.assertEqual(EXPECTED_KEYS, list(payload.keys()))
        self.assertEqual(EXPECTED_NAME, payload["name"])
        self.assertEqual(EXPECTED_VERSION, payload["version"])
        self.assertEqual(EXPECTED_STATUS, payload["status"])
        self.assertRegex(payload["timestamp"], TIMESTAMP_PATTERN)
        head, separator, body = raw_request(port, b"HEAD /health")
        self.assertEqual(b"\r\n\r\n", separator, "HEAD /health head")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), "HEAD /health status")
        self.assertEqual(b"", body)
        for request_line, status, expected, with_host in (
            (b"GET /nope", b"HTTP/1.1 404", NOT_FOUND_BODY, True),
            (b"POST /health", b"HTTP/1.1 405", METHOD_NOT_ALLOWED_BODY, True),
            (b"GET /health", b"HTTP/1.1 400", BAD_REQUEST_BODY, False),
        ):
            context = request_line.decode("ascii")
            head, separator, body = raw_request(
                port, request_line, with_host=with_host
            )
            self.assertEqual(b"\r\n\r\n", separator, context)
            self.assertTrue(
                head.startswith(status),
                "%s status line was %r"
                % (context, head.split(b"\r\n", 1)[0]),
            )
            self.assertEqual(expected, body, context)
            self.assertNotIn(b"text/html", head.lower(), context)
        # 405 is the one refusal that owes an Allow field. The field name is
        # matched case-insensitively and its value compared exactly.
        head = raw_request(port, b"POST /health")[0]
        self.assertEqual(
            [EXPECTED_ALLOW.encode("ascii")],
            [
                field.split(b":", 1)[1].strip()
                for field in head.split(b"\r\n")[1:]
                if field.lower().startswith(b"allow:")
            ],
            "the Allow field of a 405 from the --serve child",
        )

    @staticmethod
    def _port_accepts(port):
        return ExistingBehaviourTest._port_settles(port, accepting=True)

    @staticmethod
    def _port_is_released(port):
        return ExistingBehaviourTest._port_settles(port, accepting=False)

    @staticmethod
    def _port_settles(port, accepting):
        """Polls a port until it is or is not accepting, within the bound.

        Polled rather than probed once because the bind and the close both
        happen in another process, so a single attempt would be a race.
        """
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while True:
            try:
                socket.create_connection(
                    (LOOPBACK_HOST, port), timeout=REQUEST_TIMEOUT
                ).close()
                reached = True
            except OSError:
                reached = False
            if reached == accepting:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(FRESHNESS_POLL_INTERVAL)

    @staticmethod
    def _run(*arguments):
        """Runs this interpreter on ``arguments`` from the module's directory.

        ``-B`` keeps the child from writing bytecode beside the sources, so a
        test is never what dirties the tree. The run is bounded, so a child
        that blocks -- as a listener started without the flag would -- fails a
        test instead of stalling the suite.
        """
        return subprocess.run(
            [sys.executable, "-B"] + list(arguments),
            cwd=APP_DIRECTORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=REQUEST_TIMEOUT,
        )


class PayloadAndConfigurationTest(unittest.TestCase):
    def test_the_health_payload_reports_the_four_contract_fields_in_order(self):
        payload = app.health_payload()
        self.assertEqual(EXPECTED_KEYS, list(payload.keys()))
        self.assertEqual(EXPECTED_FIELD_COUNT, len(payload))
        self.assertEqual(EXPECTED_NAME, payload["name"])
        self.assertEqual(app.APP_NAME, payload["name"])
        self.assertEqual(EXPECTED_VERSION, payload["version"])
        self.assertEqual(app.APP_VERSION, payload["version"])
        self.assertEqual(EXPECTED_STATUS, payload["status"])
        for key in EXPECTED_KEYS:
            # Including the version, which a reader could be tempted to serve
            # as a number.
            self.assertIsInstance(payload[key], str, "field %s" % key)
        # A fresh document per call: a shared module-level dict would let one
        # caller mutate every later answer, and could not carry a per-request
        # timestamp anyway.
        payload["status"] = "DOWN"
        self.assertEqual(EXPECTED_STATUS, app.health_payload()["status"])
        self.assertRegex(app.current_timestamp(), TIMESTAMP_PATTERN)
        self.assertRegex(app.health_payload()["timestamp"], TIMESTAMP_PATTERN)

    def test_a_malformed_port_or_host_falls_back_instead_of_raising(self):
        # An operator who mistypes PORT gets a running endpoint on the
        # documented default, never a traceback.
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
        # Loopback unless an operator opts in: an unset or blank HOST read as
        # "every interface" is the one mistake here that would put the endpoint
        # on the network.
        self.assertEqual(app.DEFAULT_HOST, resolved_host(None))
        self.assertEqual(app.DEFAULT_HOST, resolved_host(""))
        self.assertEqual(app.DEFAULT_HOST, resolved_host("   "))
        self.assertEqual("127.0.0.1", app.DEFAULT_HOST)
        self.assertEqual("127.0.0.2", resolved_host("127.0.0.2"))
        self.assertEqual("127.0.0.2", resolved_host("  127.0.0.2  "))


class HealthEndpointContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            server = app.create_server(LOOPBACK_HOST, EPHEMERAL_PORT)
        except OSError as error:
            raise AssertionError(
                "the suite could not bind an ephemeral loopback port: %s"
                % (error,)
            ) from error
        try:
            host, port = server.server_address[:2]
            # Which number the operating system hands out is not ours to
            # constrain, so only loopback and "a port was assigned" are
            # asserted. The third clause proves the explicit 0 reached the
            # socket: ``create_server`` tests its arguments with ``is None`` so
            # a falsy port still counts, and a truthiness test there would
            # silently bind the fixed default and compete for it.
            if host != LOOPBACK_HOST or port <= 0 or port == app.DEFAULT_PORT:
                raise AssertionError(
                    "the suite bound %r:%r instead of an ephemeral loopback "
                    "port" % (host, port)
                )
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

    @classmethod
    def tearDownClass(cls):
        # In this order: shutdown() ends the serve_forever loop, server_close()
        # releases the socket and joins the handler threads, and the join proves
        # the serving thread ended. Nothing may outlive the run.
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=REQUEST_TIMEOUT)
        if cls.server_thread.is_alive():
            raise AssertionError(
                "the serving thread outlived shutdown(); a listener would "
                "survive the test run"
            )

    def _send(self, path, method=None, data=None):
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method
        )
        return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)

    def _read(self, path, method=None, data=None):
        response = self._send(path, method=method, data=data)
        try:
            return response.status, response.headers, response.read()
        finally:
            response.close()

    def _read_error(self, path, method=None, data=None):
        """Returns ``(code, headers, body)`` for a request that must be refused.

        urllib raises ``HTTPError`` for a 4xx and that exception *is* the
        response, so status, headers and body are all read back from it.
        """
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._send(path, method=method, data=data)
        error = caught.exception
        try:
            return error.code, error.headers, error.read()
        finally:
            error.close()

    def _raw_exchange(self, request_line, with_host=True):
        port = self.server.server_address[1]
        header_block, separator, body = raw_request(
            port, request_line, with_host=with_host
        )
        self.assertEqual(
            b"\r\n\r\n", separator, "the response head was never terminated"
        )
        return header_block, body

    def _exchange_verbatim(self, raw):
        """Sends ``raw`` byte for byte and returns the whole reply.

        :func:`raw_request` composes its message; this sends exactly what it is
        given, so the request version and header block are the test's to
        choose. Every response closes its connection, so EOF ends the reply.
        """
        connection = socket.create_connection(
            self.server.server_address[:2], timeout=REQUEST_TIMEOUT
        )
        try:
            connection.sendall(raw)
            received = b""
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received += chunk
        finally:
            connection.close()
        return received

    def _assert_a_raw_refusal(
        self, request_line, status, expected_body, with_host=True
    ):
        context = request_line.decode("latin1")
        head, received = self._raw_exchange(
            request_line, with_host=with_host
        )
        self.assertTrue(
            head.startswith(b"HTTP/1.1 " + status),
            "%s: status line was %r"
            % (context, head.split(b"\r\n", 1)[0]),
        )
        lowered = head.lower()
        self.assertIn(b"content-type: application/json", lowered, context)
        self.assertNotIn(b"text/html", lowered, context)
        self.assertIn(
            b"cache-control: %s" % EXPECTED_CACHE_CONTROL.encode("ascii"),
            lowered,
            context,
        )
        self.assertIn(
            b"content-length: %d" % len(expected_body), lowered, context
        )
        self.assertNotIn(b"\r\nallow:", lowered, context)
        method, _, target = context.partition(" ")
        self.assertEqual(
            b"" if method == "HEAD" else expected_body, received, context
        )
        # The whole point of a fixed body: nothing the caller sent comes back.
        for sent in (target.encode("latin1"), method.encode("latin1")):
            if sent not in (b"GET", b"HEAD", b"/health"):
                self.assertNotIn(sent, received, context)
                self.assertNotIn(sent, head.split(b"\r\n", 1)[1], context)

    def _assert_the_common_headers(self, headers, body, context):
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

        Grammar alone cannot tell a per-request timestamp from one captured at
        import time, so the answer has to be seen changing. The wait is
        bounded, so a stuck timestamp fails instead of hanging the run.
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
        announced_length = header_value(headers, "Content-Length")
        self.assertNotEqual("", announced_length, "Content-Length is mandatory")
        self.assertEqual(len(raw), int(announced_length))
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
        server = header_value(headers, "Server")
        self.assertNotIn("python", server.lower())
        self.assertNotIn(sys.version.split()[0], server)
        self.assertEqual(EXPECTED_SERVER, server.strip())
        wire_fields = self._raw_exchange(b"GET /health")[0].split(b"\r\n")
        server_fields = [
            field
            for field in wire_fields
            if field.lower().startswith(b"server:")
        ]
        self.assertEqual(
            [b"Server: " + EXPECTED_SERVER.encode("ascii")],
            [field.rstrip() for field in server_fields],
            "the Server field on the wire was %r" % server_fields,
        )
        probed_status, _, probed_raw = self._read("/health?probe=lb")
        self.assertEqual(200, probed_status)
        self.assertEqual(EXPECTED_KEYS, list(json.loads(probed_raw).keys()))
        later = self._await_a_later_timestamp(payload["timestamp"])
        self.assertRegex(later, TIMESTAMP_PATTERN)
        self.assertNotEqual(payload["timestamp"], later)

    def test_head_health_returns_the_get_headers_without_a_body(self):
        get_status, get_headers, get_body = self._read("/health")
        self.assertEqual(200, get_status)
        head_status, head_headers, head_body = self._read("/health", method="HEAD")
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
        announced_length = header_value(head_headers, "Content-Length")
        self.assertNotEqual("", announced_length, "Content-Length is mandatory")
        self.assertGreater(int(announced_length), 0)
        self.assertEqual(len(get_body), int(announced_length))
        self.assertEqual(b"", head_body)
        # http.client knows a HEAD response has no body and stops reading at the
        # header terminator, so a server that wrongly wrote one would still read
        # back empty through urllib. This exchange counts the bytes that really
        # followed the terminator.
        header_block, wire_body = self._raw_exchange(b"HEAD /health")
        self.assertTrue(
            header_block.startswith(b"HTTP/1.1 200"),
            "status line was [%r]" % (header_block.split(b"\r\n", 1)[0],),
        )
        self.assertIn(
            b"content-length: %d" % len(get_body), header_block.lower()
        )
        self.assertEqual(b"", wire_body)

    def _abort(self, request=None):
        """Opens a connection, optionally sends ``request``, then resets it.

        ``SO_LINGER`` with a zero timeout turns the close into a TCP RST, so
        the server's next read or write really fails. With ``request`` the abort
        lands while the response is written, without it while the request is
        still being read -- the two paths a giving-up probe takes.
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
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while (
            threading.active_count() > baseline_threads
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

    def test_aborted_callers_and_refused_requests_disclose_nothing(self):
        host, port = self.server.server_address[:2]
        request = (
            b"GET /health HTTP/1.1\r\nHost: "
            + ("%s:%d" % (host, port)).encode("ascii")
            + b"\r\n\r\n"
        )
        baseline_threads = threading.active_count()
        captured = io.StringIO()
        # The disclosure under test is written to stderr by socketserver's own
        # handle_error -- the peer's address, then a full traceback -- so the
        # stream is captured for the duration rather than inspected after.
        with contextlib.redirect_stderr(captured):
            for _ in range(ABORT_ATTEMPTS):
                self._abort(request)
            for _ in range(ABORT_ATTEMPTS):
                self._abort()
            # Connections are accepted in order, so an answered request proves
            # the endpoint survived the aborts and that every aborted
            # connection has reached a handler thread before the wait below.
            status, _, raw = self._read("/health")
            self._wait_for_handlers(baseline_threads)
        self.assertEqual(
            "",
            captured.getvalue(),
            "an aborting client made the server write to standard error",
        )
        self.assertEqual(200, status)
        self.assertEqual(EXPECTED_KEYS, list(json.loads(raw).keys()))
        code, headers, body = self._read_error("/nope")
        self.assertEqual(404, code)
        self._assert_the_common_headers(headers, body, "path /nope")
        self.assertEqual(NOT_FOUND_BODY, body)
        self.assertNotIn(b"nope", body)
        for method, data in REJECTED_METHODS:
            # Without the send_error override in app.py, http.server answers an
            # unsupported verb with 501 and a stock text/html page whose body
            # repeats the caller's own method back at them.
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
        for method, data in REJECTED_METHODS:
            context = "method %s on path /nope" % method
            code, headers, body = self._read_error(
                "/nope", method=method, data=data
            )
            self.assertEqual(404, code, context)
            self.assertEqual("", header_value(headers, "Allow"), context)
            self._assert_the_common_headers(headers, body, context)
            self.assertEqual(NOT_FOUND_BODY, body, context)
            self.assertNotIn(method.encode("ascii"), body, context)
            self.assertNotIn(b"nope", body, context)
        for request_line in RAW_FOREIGN_REQUEST_LINES:
            self._assert_a_raw_refusal(request_line, b"404", NOT_FOUND_BODY)
        for request_line in MISSING_HOST_REQUEST_LINES:
            self._assert_a_raw_refusal(
                request_line, b"400", BAD_REQUEST_BODY, with_host=False
            )
        for label, raw in (
            ("HTTP/1.0 without Host", b"GET /health HTTP/1.0\r\n\r\n"),
            ("an empty Host", b"GET /health HTTP/1.1\r\nHost:\r\n\r\n"),
            ("a lower-case host", b"GET /health HTTP/1.1\r\nhost: x\r\n\r\n"),
        ):
            served = self._exchange_verbatim(raw)
            self.assertTrue(
                served.startswith(b"HTTP/1.1 200"),
                "%s must still be served; status line was %r"
                % (label, served.split(b"\r\n", 1)[0]),
            )
        for error in PEER_DISCONNECT_ERRORS:
            with self.subTest(error=type(error).__name__):
                writer = AbortedPeerWriter(error)
                handler = DetachedHandler(writer)
                handler._send_json(HTTPStatus.OK, app.health_payload())
                self.assertGreaterEqual(writer.writes, 1)
                self.assertTrue(handler.close_connection)
        # The guard has to stay narrow: hiding a failure that is not a peer
        # disconnect would turn a defect on the response path into silence.
        writer = AbortedPeerWriter(ValueError("not a peer disconnect"))
        handler = DetachedHandler(writer)
        with self.assertRaises(ValueError):
            handler._send_json(HTTPStatus.OK, app.health_payload())


if __name__ == "__main__":
    unittest.main()
