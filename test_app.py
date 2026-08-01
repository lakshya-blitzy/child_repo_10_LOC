"""Standard-library test suite for the ``/health`` endpoint of :mod:`app`.

Run it from this directory, with the bytecode cache directed out of the tree::

    PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m unittest

That prefix is part of the command rather than a refinement of it. Importing or
compiling a module makes CPython write its bytecode beside the source by
default, so a bare invocation leaves an untracked ``__pycache__`` directory
in a repository whose tree is expected to stay clean -- and it is the test
runner itself that writes it, before any test code could prevent it. Directing
the cache elsewhere is the only way the suite can be run without touching the
tree; ``PYTHONDONTWRITEBYTECODE=1`` and ``python3 -B -m unittest`` have the
same effect by not writing it at all.

Six tests across three cases, built only from the standard library: no test
framework to install, no runner configuration, and no third-party client. Every
server this suite binds -- the one it hosts in-process and the one it starts as
a child process through ``app.py --serve`` -- is bound on port 0, so the suite
takes ephemeral ports and never collides with a running ``--serve`` or with the
sibling suites at the other two levels of the composition. Nothing here mutates
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
# the interpreter version is never advertised, and ``BaseHTTPRequestHandler``
# composes the field value as ``server_version + " " + sys_version`` -- so what
# arrives is this application's name and version followed by the separator the
# emptied half leaves behind. RFC 9110 excludes leading and trailing whitespace
# from a field value and lets a recipient strip it, so the value is asserted
# stripped, and once more against the bytes on the wire.
EXPECTED_SERVER = "child_repo_10_LOC/1.0.0"

# The fixed error bodies. None may ever grow to include the path that was asked
# for, the method that was used, or a field the request failed to send.
NOT_FOUND_BODY = b'{"error":"Not Found"}'
METHOD_NOT_ALLOWED_BODY = b'{"error":"Method Not Allowed"}'
BAD_REQUEST_BODY = b'{"error":"Bad Request"}'

# Request lines that must all be answered 404, and that only a raw socket can
# send: urllib rewrites or refuses every one of them. Each names a target that
# some part of the stack could be tempted to read as the route -- a run of
# leading slashes, a target whose parsed path is empty, one that resolves to the
# route only after ``..`` is applied, and the absolute form -- and the contract
# is that the target is compared exactly as it arrived, so none of them is the
# route. ``POST ///health`` is included because a foreign target must be refused
# for what it is before the method is looked at.
RAW_FOREIGN_REQUEST_LINES = (
    b"GET ///health",
    b"GET //health",
    b"GET //health/x",
    b"GET /a/../health",
    b"GET http://127.0.0.1/health",
    b"POST ///health",
)

# Request lines sent without a ``Host`` field, which RFC 9112 requires of every
# HTTP/1.1 message. All four must be answered with the one fixed 400, whatever
# the target and whatever the method: the field is checked before either.
MISSING_HOST_REQUEST_LINES = (
    b"GET /health",
    b"HEAD /health",
    b"GET /nope",
    b"FOO /health",
)

# The startup line ``--serve`` prints, and the whole of it: the application's own
# name and version, the address it really bound, and the route. Nothing else --
# no interpreter version, no path, no process id.
BANNER_PATTERN = re.compile(
    r"^child_repo_10_LOC 1\.0\.0 listening on "
    r"http://127\.0\.0\.1:(\d+)/health$"
)

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


def environment(name, value):
    """Returns a mapping naming one variable, or an empty one when ``value`` is
    ``None``.

    The resolvers read the mapping they are given, so a case is expressed as a
    literal dictionary rather than by writing into ``os.environ``. That matters
    for more than tidiness: ``os.environ`` is process-wide, so a test that set
    it would be visible to the server thread this file runs, to any library
    that reads the environment while the case is in flight, and to every
    subprocess started afterwards -- and a failure between the write and its
    restoration would leak into the rest of the run.
    """
    return {} if value is None else {name: value}


def resolved_port(value):
    """Returns the port ``PORT=value`` resolves to; ``None`` means unset."""
    return app.resolve_port(environment("PORT", value))


def resolved_host(value):
    """Returns the address ``HOST=value`` resolves to; ``None`` means unset."""
    return app.resolve_host(environment("HOST", value))


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


def raw_request(port, request_line, with_host=True):
    """Speaks one HTTP/1.1 exchange over a socket and splits the reply.

    Returns ``(header_block, separator, body)`` with the head and the body
    separated at the first CRLF pair, so a caller can both prove the head was
    terminated and count the bytes that genuinely followed the terminator.
    ``Connection: close`` makes the server end the stream, which is what lets
    the whole reply be read to EOF without interpreting ``Content-Length``
    first -- often the very header under test.

    A socket rather than urllib, because several of the request forms this
    contract has to answer cannot be expressed through a client at all: urllib
    supplies a ``Host`` field of its own, so its absence is unaskable, and it
    rewrites or refuses a target such as ``//health`` before it reaches the
    wire. ``with_host`` set to ``False`` is what makes the missing field
    testable.

    Response fields are read case-insensitively by the caller, never here: the
    field name's casing is not part of the contract even when the reply is read
    as raw bytes.
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
    """The capability this repository had before the health endpoint existed,
    and the gate that keeps it: the ``--serve`` flag, whose whole purpose is
    that the default invocation stayed exactly as it was. Both branches of that
    gate are therefore exercised here, each in a real child process.
    """

    def test_the_greeting_is_unchanged_and_only_serve_starts_a_listener(self):
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
        completed = self._run(APP_PATH)
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
        # Importing the module must be silent as well, and this file's own
        # ``import app`` cannot show that: by the time any test runs the module
        # is already in ``sys.modules``, and whatever it printed at import
        # time went to the runner's streams before the first test was even
        # collected. So a fresh interpreter is asked to import it and nothing
        # else. A print or a banner at module scope fails here, as does a
        # listener started at import: the process would never reach its own
        # exit and the timeout would end the test instead.
        imported = self._run("-c", "import app")
        self.assertEqual(b"", imported.stdout)
        self.assertEqual(b"", imported.stderr)
        self.assertEqual(0, imported.returncode)
        # The other branch of the same gate, and the only part of this module an
        # operator ever invokes that nothing above reaches: ``serve()``, its
        # startup banner, its bind, its request loop and its shutdown. Calling
        # ``create_server`` -- which is what the contract case below does -- can
        # prove none of them: it neither prints the banner, nor reads the
        # environment, nor runs the loop, nor releases anything. So the program
        # is started the documented way, in its own process, and made to answer.
        self._assert_serve_binds_serves_and_releases()

    def _assert_serve_binds_serves_and_releases(self):
        """Starts ``app.py --serve``, proves it serves, then stops it.

        ``PORT=0`` is passed so the child takes an ephemeral port and never
        competes for the documented default with a developer's own server, and
        ``HOST`` is removed from the child's environment so the loopback default
        is what it binds. The environment is a copy: this suite never writes
        into ``os.environ``, which every thread and later subprocess would see.

        The port the child announces is the port it is then probed on, so a
        process that printed a banner and died -- or one that bound something
        other than what it announced -- fails here rather than passing.
        """
        environ = dict(os.environ, PORT="0", PYTHONDONTWRITEBYTECODE="1")
        environ.pop("HOST", None)
        # ``with`` closes the child's pipes and reaps it however this method
        # leaves, so a failed assertion cannot leave a file descriptor or a
        # zombie behind; the inner ``finally`` kills a child that is still
        # running first, because the context manager's own wait is unbounded.
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
        # The banner was the whole of the child's standard output: no access log,
        # no shutdown notice, no second line.
        self.assertEqual(b"", remaining_stdout)

    def _exercise_and_stop(self, child):
        """Probes a running ``--serve`` child, then interrupts it.

        Returns the child's remaining standard output and its standard error.
        The port it announced is asserted released before returning, because the
        release is part of the shutdown this exercises.
        """
        try:
            banner = self._first_line(child)
            matched = BANNER_PATTERN.match(banner)
            self.assertIsNotNone(
                matched, "the startup banner was %r" % (banner,)
            )
            port = int(matched.group(1))
            # An assigned ephemeral port is never the documented default, which
            # is what proves PORT=0 reached the socket rather than being read as
            # "unset" and silently replaced by the fallback.
            self.assertGreater(port, 0)
            self.assertNotEqual(app.DEFAULT_PORT, port)
            # Something is listening on the port that was announced. Asserted
            # before the contract probes so that a child which printed its
            # banner and then died -- the exact failure a banner-only check
            # cannot see -- is reported as that, rather than as a bare
            # ConnectionRefusedError from the first request.
            self.assertTrue(
                self._port_accepts(port),
                "the child announced port %d but nothing accepted a connection "
                "there" % port,
            )
            # The contract, on the port the child itself named. Every shape the
            # endpoint owes is asked for, because a listener that answers only
            # its happy path is not the endpoint this repository documents.
            self._assert_the_child_serves_the_contract(port)
            # It was still running while it served, so what answered was the
            # child and not something left over from an earlier run.
            self.assertIsNone(child.poll(), "the child exited while serving")
            # SIGINT is what an operator sends with Ctrl-C, and app.py catches
            # it so the loop ends, the socket is closed and the process exits
            # cleanly. Asserting the status and the streams is what proves that
            # path runs: an uncaught KeyboardInterrupt would exit non-zero with
            # a traceback naming this repository's absolute paths on stderr.
            child.send_signal(signal.SIGINT)
            streams = child.communicate(timeout=REQUEST_TIMEOUT)
        finally:
            if child.poll() is None:
                child.kill()
        # The socket really went with the process, so a later run can have the
        # port back. Polled rather than asserted once, because the close and this
        # test happen in different processes.
        self.assertTrue(
            self._port_is_released(port),
            "port %d was still accepting connections after shutdown" % port,
        )
        return streams

    @staticmethod
    def _first_line(child):
        """Returns the child's first line of standard output, decoded.

        Read in a daemon thread so that a child which neither prints nor exits
        fails this test on a bound instead of hanging the whole run; a daemon
        cannot keep the interpreter alive if it is still blocked when the suite
        ends. An empty string is returned when the child produced nothing, which
        fails the banner match with a readable message.
        """
        captured = []

        def read():
            try:
                captured.append(child.stdout.readline())
            except (OSError, ValueError):
                # The pipe was closed while this thread was still blocked on it,
                # which can only happen once the bound below has expired and the
                # test is failing anyway. Letting it out of a daemon thread would
                # add noise to that failure rather than information.
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
        """Asserts every answer the child owes, over a raw socket.

        A served ``GET``, a bodiless ``HEAD``, and the three refusals: a target
        that is not the route, a method the route does not allow, and a message
        with no ``Host`` field.
        """
        # A served GET, and the document itself rather than only its status:
        # this is the one place the whole path from the documented command to the
        # wire is exercised end to end.
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
        # HEAD, whose header set is the GET's and whose body is nothing at all.
        head, separator, body = raw_request(port, b"HEAD /health")
        self.assertEqual(b"\r\n\r\n", separator, "HEAD /health head")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), "HEAD /health status")
        self.assertEqual(b"", body)
        # The three refusals, each with its fixed body: a target that is not the
        # route, a method the route does not allow, and a message with no Host.
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
        # 405 is the one refusal that owes an Allow field, and the child must
        # send it just as the in-process server does. The field name is matched
        # case-insensitively and its value compared exactly, because the ``", "``
        # spacing is part of the contract all three implementations share.
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
        """Polls until the port accepts a connection, or the bound expires."""
        return ExistingBehaviourTest._port_settles(port, accepting=True)

    @staticmethod
    def _port_is_released(port):
        """Polls until the port refuses connections, or the bound expires."""
        return ExistingBehaviourTest._port_settles(port, accepting=False)

    @staticmethod
    def _port_settles(port, accepting):
        """Polls a port until it is or is not accepting, within the bound.

        Both directions are polled rather than probed once, because the bind and
        the close both happen in another process: a single attempt would be a
        race that this suite could lose either way.
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

        ``-B`` keeps the child from writing a ``__pycache__`` directory beside
        the sources: this repository's working tree is expected to stay clean,
        and a test must not be what dirties it. The run is bounded, so a child
        that blocks -- which is what a listener started without the flag would
        do -- fails a test instead of stalling the suite.
        """
        return subprocess.run(
            [sys.executable, "-B"] + list(arguments),
            cwd=APP_DIRECTORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=REQUEST_TIMEOUT,
        )


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
            #
            # The third clause is what proves the explicit 0 reached the socket
            # rather than being read as "unset": ``create_server`` tests its
            # arguments with ``is None`` precisely so a falsy port still
            # counts as a request, and were that ever changed to a truthiness
            # test this class would silently fall back to the fixed default
            # port and compete for it with a developer's own running server. An
            # assigned ephemeral port is never the default one, so that
            # comparison settles it without asking for a second port --
            # releasing one and rebinding it by number would be a race another
            # process could win.
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

    def _raw_exchange(self, request_line, with_host=True):
        """Returns ``(header_block, body)`` for one exchange over a socket.

        Delegates to :func:`raw_request` against the port this class bound, and
        asserts here what every reply owes regardless of its status: a
        terminated head.
        """
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

        :func:`raw_request` composes the message it sends, which is what most
        cases want; this sends exactly the bytes given, so the request version
        and the header block are the test's to choose. Every response from this
        endpoint closes its connection, so reading to EOF is complete.
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
        """Asserts one raw request's whole refusal: status, envelope, silence.

        The head is checked for the status, for the JSON media type, for the
        cache directive, for a byte-accurate length and for the absence of
        markup; the body is compared to the fixed literal it must be, and then
        searched for every field of the request that must not have come back --
        the target and the method it was sent with.
        """
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
        # Neither a 404 nor a 400 owes an Allow field, and neither may send one:
        # naming a method that would have worked would be an answer about an
        # address this endpoint does not serve, or about a message it never
        # accepted. Only the 405 branch advertises it.
        self.assertNotIn(b"\r\nallow:", lowered, context)
        # HEAD carries the refusal's headers and none of its body, exactly as it
        # does on the success path.
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
        # The handler empties ``sys_version``, so the Server header names this
        # application and its version and discloses nothing about the runtime it
        # happens to be built on -- neither the interpreter's name nor the
        # version this very process is running. Compared stripped, because the
        # base class joins the two halves of the value with a space and the
        # emptied half leaves that separator at the end, which RFC 9110 lets a
        # recipient discard.
        server = header_value(headers, "Server")
        self.assertNotIn("python", server.lower())
        self.assertNotIn(sys.version.split()[0], server)
        self.assertEqual(EXPECTED_SERVER, server.strip())
        # And compared again against the bytes that actually arrived, because a
        # parser is entitled to strip what it received. Collecting every Server
        # line makes this an exact-value assertion that also proves there is
        # exactly one of them.
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
        # The same unsupported methods against a path that is not the route.
        # This is the precedence proof: the target is judged before the method,
        # so a request that is wrong in both ways is a 404 and not a 405 -- and
        # it must not advertise Allow, which would tell a caller that some method
        # would have worked on an address this endpoint does not serve.
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
        # Every target a client library cannot even send: a run of leading
        # slashes, an empty parsed path, a ``..`` segment that resolves to the
        # route, and the absolute form. The contract compares the target exactly
        # as it arrived, so all of them are 404 -- which is also what the
        # JavaScript and Java siblings answer, so a probe cannot tell the three
        # implementations apart by asking for an unadvertised spelling.
        for request_line in RAW_FOREIGN_REQUEST_LINES:
            self._assert_a_raw_refusal(request_line, b"404", NOT_FOUND_BODY)
        # And every message that omitted the Host field RFC 9112 requires of an
        # HTTP/1.1 request: one fixed 400 whatever the target and whatever the
        # method, because the field is checked before either is looked at.
        for request_line in MISSING_HOST_REQUEST_LINES:
            self._assert_a_raw_refusal(
                request_line, b"400", BAD_REQUEST_BODY, with_host=False
            )
        # The rule belongs to HTTP/1.1 alone, and only the absent field breaks
        # it. An HTTP/1.0 request owes no Host and is served; a field that
        # arrived empty was sent, so it is served; and the field name is matched
        # case-insensitively, exactly as the sibling implementations match it.
        # All three are asserted so the check above cannot be over-broad.
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
