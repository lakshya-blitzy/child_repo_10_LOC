"""Hand-run ``unittest`` suite for this repository's health endpoint.

Two things are proved here. The first is that the application still does what it
always did: ``greet`` is asserted for the first time in this repository's
history, which only became possible once the duplicated ``__main__`` guard was
removed from ``app.py`` and the module could be imported at all. The second is
that ``GET /health`` honours the response contract shared by all three
applications of this composition -- the four-field document, the three mandated
headers, ``HEAD``, and the fixed error envelopes.

Only the standard library is used, so this repository keeps its zero-dependency
posture: no test framework, no HTTP client library, no runner configuration and
nothing to install before the suite runs. ``socket`` is here for one assertion
that no HTTP client can make -- proving that a ``HEAD`` response really put zero
bytes on the wire -- because ``http.client`` discards a HEAD body without ever
reporting it. Discovery is left to the defaults, which is why the file is named
``test_app.py`` and sits beside ``app.py``:

    python3 -m unittest          # from this directory: 6 tests, OK
    python3 test_app.py          # the same suite, through the footer below

The server under test is bound on port 0, so the operating system hands out an
ephemeral port. That is what lets the suite run beside an already-running
``python3 app.py --serve``, and beside the sibling suites at the other two
levels of the composition, without ever colliding on a port. Nothing here reads
or writes an environment variable, so a run never depends on the shell that
started it, and nothing here calls ``app.serve()``, which would print a banner
and block forever.

Expected values are spelled out as literals rather than read back from ``app``.
Asserting the published contract instead of mirroring the implementation is what
lets this suite catch a change made on either side of it, and it is what keeps
the three independent implementations of the same four-field payload -- one per
level, because the three repositories share no code -- from drifting apart
unnoticed.
"""

import json
import re
import socket
import threading
import unittest
import urllib.error
import urllib.request

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

# The address the server under test is bound to. Port 0 is deliberate: the
# operating system assigns an ephemeral port, and the port actually assigned is
# read back from the bound server rather than assumed.
LOOPBACK_HOST = "127.0.0.1"
EPHEMERAL_PORT = 0


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
        # that adding an endpoint preserved it. This could not be asserted at
        # all until app.py became importable: py_compile used to exit 1 on the
        # duplicated __main__ guard, so no test could reach greet.
        self.assertEqual("Hello Lakshya", app.greet("Lakshya"))
        # The default program prints greet("Lakshya"), so that exact call is the
        # one that matters; a second name proves the greeting is still built
        # from its argument rather than from a captured constant.
        self.assertEqual("Hello world", app.greet("world"))


class HealthPayloadTest(unittest.TestCase):
    """The health document itself, asserted without involving HTTP at all."""

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

    def test_the_timestamp_is_a_second_precision_utc_instant(self):
        # Node truncates and Java truncates; Python formats. Three mechanisms,
        # one grammar -- asserted here so this level cannot drift from the other
        # two while still looking plausible on its own.
        self.assertRegex(app.current_timestamp(), TIMESTAMP_PATTERN)
        self.assertRegex(app.health_payload()["timestamp"], TIMESTAMP_PATTERN)


class HealthEndpointContractTest(unittest.TestCase):
    """The live endpoint, exercised over HTTP against a really bound socket."""

    @classmethod
    def setUpClass(cls):
        # Explicit arguments, never the environment, so a run cannot depend on
        # the shell that started it. The class binds once for all three of its
        # tests.
        cls.server = app.create_server(LOOPBACK_HOST, EPHEMERAL_PORT)
        host, port = cls.server.server_address[:2]
        cls.base_url = "http://%s:%d" % (host, port)
        # The explicit 0 has to reach the socket -- create_server tests its
        # arguments with ``is None`` precisely so that it does. Were 0 ever
        # treated as "unset" instead, the suite would quietly fall back to the
        # fixed default port and start competing for it with a developer's own
        # running server, so both halves of the ephemeral property are checked
        # before any request is made: the port handed out is not the default,
        # and a second bind asking for 0 is given a different one.
        if port <= 0 or port == app.DEFAULT_PORT:
            raise AssertionError(
                "an explicit ephemeral port was not honoured; the server bound "
                "%r instead" % (port,)
            )
        spare = app.create_server(LOOPBACK_HOST, EPHEMERAL_PORT)
        spare_port = spare.server_address[1]
        spare.server_close()
        if spare_port == port:
            raise AssertionError(
                "two ephemeral binds were given the same port %r, so the port "
                "is fixed rather than assigned" % (spare_port,)
            )
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

    def test_unknown_paths_and_unsupported_methods_return_fixed_json_envelopes(self):
        code, headers, body = self._read_error("/nope")
        self.assertEqual(404, code)
        self.assertEqual(
            EXPECTED_MEDIA_TYPE, header_value(headers, "Content-Type")
        )
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
            content_type = header_value(headers, "Content-Type")
            self.assertEqual(EXPECTED_MEDIA_TYPE, content_type, context)
            self.assertNotIn("text/html", content_type, context)
            self.assertEqual(METHOD_NOT_ALLOWED_BODY, body, context)
            self.assertNotIn(method.encode("ascii"), body, context)


if __name__ == "__main__":
    unittest.main()
