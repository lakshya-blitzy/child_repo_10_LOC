"""Standard-library ``unittest`` suite for the Level 2 (``child_repo_10_LOC``) tier.

This is the Python tier's entire automated verification surface, and the first
test file this repository has ever had.  It asserts two things, and they carry
equal weight:

1. **Preserved behaviour.**  ``greet("Lakshya")`` must still return
   ``"Hello Lakshya"``, and executing the ``app`` module under any name other
   than ``__main__`` must still print nothing.  That pair is the mechanical
   enforcement of "preserve the existing functionality": the ``/health``
   endpoint was added *beside* the original program, never on top of it, so a
   regression in ``greet`` is a failure of this feature even though the feature
   never touches ``greet``.
2. **The frozen health contract.**  Every clause that can be asserted without a
   network is asserted against :func:`health.build_payload` directly -- which is
   exactly why ``health.py`` keeps the payload builder separate from the
   listener -- and every clause that genuinely needs the wire is asserted
   against a real listener bound to an **ephemeral loopback port**.

The frozen contract, restated here as the specification these tests encode::

    GET|HEAD /health      -> 200, four-member body, in this order:
                             name, version, timestamp, status
    GET      /health?x=1  -> 200, identical (the query string is ignored)
    GET      /health/     -> 404 (the path comparison is exact)
    GET      /unknown     -> 404, small JSON error body
    POST     /health      -> 405 + Allow: GET, HEAD

    Content-Type:  application/json; charset=utf-8
    Cache-Control: no-store
    name:          child_repo_10_LOC          (this tier's identifier)
    version:       1.0.0
    timestamp:     YYYY-MM-DDTHH:MM:SS.mmmZ   (UTC, exactly 3 fractional
                                               digits, per request)
    status:        UP
    Serialization: compact separators, no insignificant whitespace

Three deliberate design decisions in this file are worth stating up front,
because each one is load-bearing:

* **The expected values are spelled out as literals here, independently of
  ``health.py``.**  Asserting ``payload["name"] == health.APP_NAME`` would be a
  tautology that passes even if the tier's identity silently changed.  Where a
  test *does* compare against ``health.py``'s own constants, it is asserting
  agreement between two independent sources, never deriving the expectation
  from the code under test.
* **No listener ever binds port 8000.**  8000 is the tier's declared serving
  port; the CI workflow starts the real server on it in a separate step.  Every
  listener here binds ``("127.0.0.1", 0)`` and reads the assigned port back, so
  this suite passes whether or not the real server is running, and never
  exposes a socket beyond loopback.
* **Zero third-party packages.**  The repository has none and the target is
  none, so there is no pytest, no plugin, no assertion library, no HTTP client
  library and no clock-freezing library.  ``unittest`` plus the standard library
  is the whole toolchain, and this file is auto-discovered by a bare
  ``python -m unittest`` because it is named ``test_app.py``.

Run it directly with ``python test_app.py``, or exactly as CI does::

    python -m py_compile test_app.py     # static gate
    python -m unittest -v                # discovery: matches test*.py
"""

import contextlib
import http.client
import importlib.util
import io
import json
import pathlib
import re
import runpy
import tempfile
import threading
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app
import health
from app import greet

# ---------------------------------------------------------------------------
# Expected values
#
# Spelled independently of the code under test.  A test that reads its
# expectation out of the implementation cannot fail when the implementation
# changes, which makes it documentation rather than a gate.
# ---------------------------------------------------------------------------

#: This tier's declared identity, character for character.
EXPECTED_NAME = "child_repo_10_LOC"

#: The cross-tier version-consistency value: a SemVer *string*, never a number.
EXPECTED_VERSION = "1.0.0"

#: The literal status value, exact case.
EXPECTED_STATUS = "UP"

#: The four body members in their frozen wire order.  A ``list`` rather than a
#: ``set``, because the order is part of the contract.
EXPECTED_MEMBERS = ["name", "version", "timestamp", "status"]

#: Members that must never appear.  A frozen contract with an extension point is
#: a drift point, so the absence of a fifth member is asserted explicitly rather
#: than left implied by the length check.  ``host``, ``port`` and ``path`` are
#: included because they are configuration, and configuration must not leak into
#: the response body; ``checks``, ``releaseId``, ``description``, ``notes``,
#: ``output``, ``serviceId``, ``links`` and ``time`` are the optional members of
#: the health-check draft's wider vocabulary that this contract does not adopt.
FORBIDDEN_MEMBERS = (
    "checks",
    "commit",
    "description",
    "environment",
    "error",
    "host",
    "hostname",
    "links",
    "notes",
    "output",
    "path",
    "pid",
    "port",
    "releaseId",
    "serviceId",
    "time",
    "uptime",
)

#: The resource path, and the bind address the configuration file declares for
#: the real server.  Note that no test ever binds ``EXPECTED_BIND_HOST``: it is
#: asserted as a *configured value*, while listeners in this file are loopback
#: only.
EXPECTED_PATH = "/health"
# Security note for anyone enabling flake8-bandit's S104 (hardcoded-bind-all-
# interfaces): this literal is the *configured value under assertion*, never an
# address anything in this file binds.  Every listener here binds LOOPBACK_HOST.
EXPECTED_BIND_HOST = "0.0.0.0"

#: The tier's declared serving port.  Asserted as configuration, and asserted
#: *not* to be the port any listener in this suite binds.
TIER_PORT = 8000

#: Response headers the contract mandates on a success.
EXPECTED_CONTENT_TYPE = "application/json; charset=utf-8"
EXPECTED_CACHE_CONTROL = "no-store"

#: Exact value of the ``Allow`` header on a ``405``: uppercase, one comma, one
#: space, ``GET`` before ``HEAD``.
EXPECTED_ALLOW = "GET, HEAD"

#: RFC 8259 requires UTF-8, and the contract's ``charset`` parameter says so.
EXPECTED_ENCODING = "utf-8"

#: The preserved-behaviour fingerprint of the original program: one space, no
#: punctuation, this capitalization.
GREETED_NAME = "Lakshya"
EXPECTED_GREETING = "Hello Lakshya"

#: RFC 3339 UTC with millisecond precision.  Anchored by using ``fullmatch``.
#: Exactly three fractional digits and a ``Z`` suffix -- which is what catches
#: the classic ``datetime.isoformat()`` mistake, since that emits six fractional
#: digits and a ``+00:00`` offset.
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")

#: Byte-shape of the serialized body around the one member that varies.  A
#: cheap, strong check: it pins the member order, the compact separators, the
#: quoting and both fixed values in a single comparison.
SERIALIZED_PREFIX = '{"name":"child_repo_10_LOC","version":"1.0.0","timestamp":"'
SERIALIZED_SUFFIX = '","status":"UP"}'

#: ``json.dumps``'s *default* separators.  Their absence from the output is the
#: assertion that keeps this tier byte-shape identical to the JavaScript tier,
#: whose ``JSON.stringify`` is compact by default.
DEFAULT_SEPARATOR_MARKERS = (", ", ": ")

#: Loopback only, and port 0 so the operating system assigns a free port.
LOOPBACK_HOST = "127.0.0.1"
EPHEMERAL_PORT = 0

#: Pause between the two calls of a freshness assertion.  The timestamp has
#: millisecond resolution, so 20 ms is an order of magnitude more than is needed
#: to guarantee two distinct values while keeping the suite fast.
FRESHNESS_PAUSE_SECONDS = 0.02

#: Every socket operation is bounded, so a defect surfaces as a test failure
#: rather than a hung suite that blocks CI indefinitely.
REQUEST_TIMEOUT_SECONDS = 5.0

#: Readiness is established by polling -- never by a blind sleep.  The bound is
#: 200 x 5 ms = 1 s, far beyond the sub-millisecond bind this actually takes.
READINESS_ATTEMPTS = 200
READINESS_PAUSE_SECONDS = 0.005

#: ``serve_forever``'s poll interval doubles as the upper bound on how long
#: ``shutdown()`` takes to return, so the default 0.5 s is tightened here to
#: keep teardown in the tens of milliseconds.
LISTENER_POLL_INTERVAL_SECONDS = 0.05
LISTENER_JOIN_TIMEOUT_SECONDS = 5.0

#: Names under which module *copies* are executed by the helper below.  They are
#: deliberately not ``app`` or ``health``: nothing here may shadow, replace or
#: mutate the real modules.
_APP_PROBE_MODULE_NAME = "blitzy_probe_app_import"
_HEALTH_PROBE_MODULE_NAME = "blitzy_probe_health_fallback"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _execute_module_source(module_name, source_path):
    """Execute the Python file at *source_path* as a fresh, unregistered module.

    Returns ``(module, stdout_text, stderr_text)``.

    Two properties make this the right tool for both of the places it is used
    here.  It executes the file's top-level code with ``__name__`` set to
    *module_name* -- never ``"__main__"`` -- which is precisely what an ordinary
    import does, so capturing the streams around it proves whether importing the
    file is silent.  And the resulting module is **not** inserted into
    ``sys.modules``, so the real ``app`` and ``health`` modules are never
    shadowed, replaced or reloaded: the suite has no import-order dependence and
    no cross-test state leak.

    Because ``__file__`` is set from *source_path*, a module that derives paths
    from its own location -- as ``health.py`` does -- resolves them relative to
    wherever the copy was placed.  That is what lets a copy in an empty
    directory exercise the configuration-absent fallback path without touching a
    single repository file.

    Standard-stream capture uses :func:`contextlib.redirect_stdout` and
    :func:`contextlib.redirect_stderr`, which rebind ``sys.stdout``/``sys.stderr``
    and are therefore restored even if execution raises.
    """
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {source_path!r}")
    module = importlib.util.module_from_spec(spec)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        spec.loader.exec_module(module)
    return module, stdout.getvalue(), stderr.getvalue()


class _Response:
    """Immutable snapshot of one HTTP response, safe to use after the close.

    ``http.client`` hands back a live, socket-backed object; every connection in
    this suite is closed in a ``finally`` block, so the status, the headers and
    the fully-read body are copied out first and the tests assert against this
    value object instead.
    """

    #: Sorted, as the linters prefer; the constructor keeps wire order instead.
    __slots__ = ("body", "headers", "status")

    def __init__(self, status, headers, body):
        """Store the status code, the header collection and the read body bytes."""
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        """Return one header value, or ``None``.  Field names are case-insensitive."""
        return self.headers.get(name)

    def text(self):
        """Decode the body as the contract's charset."""
        return self.body.decode(EXPECTED_ENCODING)

    def json(self):
        """Parse the body as JSON, preserving member order."""
        return json.loads(self.text())


class PayloadContractAssertions:
    """Reusable assertions for the frozen payload, shared by three test classes.

    A mixin rather than a ``TestCase`` subclass on purpose: it defines no
    ``test_*`` method and must never be collected as a test in its own right.
    It relies on the ``assert*`` methods a ``TestCase`` provides, so it is only
    ever mixed in *before* :class:`unittest.TestCase` in a concrete class.

    The same three helpers assert the payload whether it came from
    :func:`health.build_payload` in-process, from an isolated copy of the module
    with no configuration files, or from a real HTTP response -- which is how
    three very different execution paths are held to one identical contract.
    """

    def assert_payload_conforms(self, payload):
        """Assert every clause of the four-member body contract."""
        self.assertIsInstance(payload, dict, "the payload must be a JSON object")
        # Order and count, asserted as a list: both are part of the contract.
        self.assertEqual(
            list(payload),
            EXPECTED_MEMBERS,
            "the payload's members must be exactly these four, in this order",
        )
        self.assertEqual(len(payload), len(EXPECTED_MEMBERS))
        # No fifth member, named explicitly so the failure message is actionable.
        for forbidden in FORBIDDEN_MEMBERS:
            self.assertNotIn(
                forbidden,
                payload,
                f"{forbidden!r} is not part of the frozen contract",
            )
        # Every value is a string -- `version` is SemVer text, not a number.
        for member in EXPECTED_MEMBERS:
            self.assertIsInstance(
                payload[member], str, f"{member!r} must be a JSON string"
            )
        self.assertEqual(payload["name"], EXPECTED_NAME)
        self.assertEqual(payload["version"], EXPECTED_VERSION)
        self.assertEqual(payload["status"], EXPECTED_STATUS)
        self.assert_timestamp_conforms(payload["timestamp"])

    def assert_timestamp_conforms(self, timestamp):
        """Assert RFC 3339 UTC with exactly millisecond precision.

        ``fullmatch`` anchors both ends, so no leading or trailing character can
        slip through.  The two follow-up assertions name the specific defect
        each one catches, because "the regex did not match" is a poor failure
        message when the cause is almost always the same two mistakes.
        """
        self.assertIsInstance(timestamp, str)
        self.assertRegex(timestamp, TIMESTAMP_PATTERN)
        self.assertIsNotNone(
            TIMESTAMP_PATTERN.fullmatch(timestamp),
            f"{timestamp!r} must match YYYY-MM-DDTHH:MM:SS.mmmZ exactly",
        )
        self.assertTrue(
            timestamp.endswith("Z"),
            "the timestamp must carry a Z suffix, not a numeric UTC offset "
            "(datetime.isoformat() emits +00:00)",
        )
        self.assertEqual(
            len(timestamp.split(".")[-1]),
            len("mmmZ"),
            "the timestamp must carry exactly three fractional digits "
            "(datetime.isoformat() emits six)",
        )

    def assert_serialization_is_compact(self, text):
        """Assert the serialized form carries no insignificant whitespace.

        This is the measured requirement, not a style preference:
        ``json.dumps`` defaults to ``", "`` and ``": "`` separators, which
        produces different *bytes* from the JavaScript tier's compact
        ``JSON.stringify`` for identical data.  ``separators=(",", ":")`` is what
        keeps all three tiers byte-shape identical.
        """
        for marker in DEFAULT_SEPARATOR_MARKERS:
            self.assertNotIn(
                marker,
                text,
                f"{marker!r} indicates default json.dumps separators; the "
                "contract mandates separators=(',', ':')",
            )

    def assert_byte_shape(self, text):
        """Assert the serialized form around its one varying member."""
        self.assertTrue(
            text.startswith(SERIALIZED_PREFIX),
            f"{text!r} must start with {SERIALIZED_PREFIX!r}",
        )
        self.assertTrue(
            text.endswith(SERIALIZED_SUFFIX),
            f"{text!r} must end with {SERIALIZED_SUFFIX!r}",
        )
        self.assert_serialization_is_compact(text)


class TestPreservedBehavior(unittest.TestCase):
    """The Level 2 regression gate: the original program still behaves exactly.

    ``from app import greet`` at the top of this module is itself the first
    assertion, and it is not a formality.  Against the unrepaired ``app.py`` --
    which carried a duplicated, two-space-indented ``__main__`` guard and a line
    of stray text -- the import raises ``IndentationError`` and this entire file
    is uncollectible.  The repair that made the module parseable is what makes
    every test below reachable.
    """

    def test_greet_returns_the_preserved_fingerprint(self):
        """``greet("Lakshya")`` returns ``"Hello Lakshya"`` -- the tier's fingerprint.

        One space, no punctuation, this capitalization.  This is the assertion
        that turns "preserve the existing functionality" from an intention into
        a gate.
        """
        self.assertEqual(greet(GREETED_NAME), EXPECTED_GREETING)

    def test_greet_interpolates_whatever_name_it_is_given(self):
        """The f-string's behaviour, asserted for a word and for the empty string.

        Nothing beyond interpolation is asserted, because the function does
        nothing else: it performs no validation, raises nothing and has no type
        checking.  Asserting a ``TypeError`` it does not raise would invent a
        requirement rather than preserve one.
        """
        self.assertEqual(greet("World"), "Hello World")
        self.assertEqual(greet(""), "Hello ")

    def test_greet_returns_a_plain_string(self):
        """The return value is ``str``, so it is directly printable and joinable."""
        self.assertIsInstance(greet(GREETED_NAME), str)

    def test_app_exposes_greet_as_its_public_surface(self):
        """``greet`` is reachable both as an attribute and as a direct import.

        ``assertIs`` proves the two spellings resolve to one object, so a caller
        cannot accidentally exercise a different function from the one this suite
        asserts.
        """
        self.assertTrue(callable(app.greet))
        self.assertIs(app.greet, greet)

    def test_executing_the_app_module_emits_nothing_on_either_stream(self):
        """The ``__main__`` guard keeps the module silent when it is imported.

        The module is executed again from its own source under a private name,
        with both standard streams captured.  ``__name__`` is therefore not
        ``"__main__"``, the guard is false, and nothing may be written -- which
        is what allows ``health.py``, this suite and any other consumer to import
        ``app`` without the greeting appearing in their output.

        The freshly executed module's ``greet`` is exercised too, proving the
        guard suppresses only the side effect and never the definition.
        """
        module, stdout_text, stderr_text = _execute_module_source(
            _APP_PROBE_MODULE_NAME, pathlib.Path(app.__file__).resolve()
        )
        self.assertEqual(stdout_text, "", "importing app must not write to stdout")
        self.assertEqual(stderr_text, "", "importing app must not write to stderr")
        self.assertEqual(module.greet(GREETED_NAME), EXPECTED_GREETING)

    def test_direct_execution_still_prints_the_greeting(self):
        """The guarded block is intact: run as ``__main__`` it greets and stops.

        :func:`runpy.run_path` executes the file exactly as the interpreter does
        for ``python app.py`` -- ``__name__`` really is ``"__main__"`` -- but
        in-process, with both streams captured and without registering ``app`` in
        ``sys.modules``.  No subprocess is involved, which the prompt requires,
        and no ``exec`` is needed, which keeps a security scan quiet.

        This is the only assertion that catches deletion of the *guarded block*
        while ``greet`` survives: the silence test above would still pass, yet
        ``python app.py`` would print nothing and the tier's regression
        fingerprint would be gone.  The single line of output must be the
        fingerprint, and nothing may reach stderr.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            namespace = runpy.run_path(
                str(pathlib.Path(app.__file__).resolve()), run_name="__main__"
            )
        self.assertEqual(stdout.getvalue(), f"{EXPECTED_GREETING}\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(namespace["__name__"], "__main__")
        self.assertEqual(namespace["greet"](GREETED_NAME), EXPECTED_GREETING)


class TestFrozenContractConstants(unittest.TestCase):
    """``health.py``'s exported constants still spell the frozen contract.

    The handler never writes a contract value inline -- it reads these constants
    -- so pinning them pins the wire behaviour of every response at once, and a
    future edit that changes a header, a status code or the member order fails
    here with an unambiguous message rather than in a downstream integration
    probe.
    """

    def test_success_headers_are_the_contract_values(self):
        """``application/json`` (not ``application/health+json``) and ``no-store``.

        ``application/json`` is deliberate: ordinary tooling -- ``curl``, ``jq``,
        the Actions runner -- parses it with no special handling.  ``no-store``
        is what guarantees a poller reads live state rather than an
        intermediary's cached copy of an earlier answer.
        """
        self.assertEqual(health.CONTENT_TYPE, EXPECTED_CONTENT_TYPE)
        self.assertEqual(health.CACHE_CONTROL, EXPECTED_CACHE_CONTROL)

    def test_only_get_and_head_are_allowed_and_the_allow_header_agrees(self):
        """The allowed set is exactly ``GET``/``HEAD``; ``Allow`` derives from it."""
        self.assertEqual(tuple(health.ALLOWED_METHODS), ("GET", "HEAD"))
        self.assertEqual(health.ALLOW_HEADER_VALUE, EXPECTED_ALLOW)

    def test_payload_keys_are_the_four_members_in_order(self):
        """The declared member order matches the contract's frozen order."""
        self.assertEqual(list(health.PAYLOAD_KEYS), EXPECTED_MEMBERS)

    def test_serialization_constants_are_compact_and_utf8(self):
        """Compact separators and UTF-8 -- the two byte-level clauses."""
        self.assertEqual(tuple(health.JSON_SEPARATORS), (",", ":"))
        self.assertEqual(health.JSON_ENCODING, EXPECTED_ENCODING)

    def test_status_codes_are_200_404_and_405(self):
        """A healthy status requires a 2xx code; the two negative paths are fixed.

        There is deliberately no 5xx anywhere in the contract: the handler reads
        already-resolved values and a clock, so it has no failure path to report.
        """
        self.assertEqual(health.STATUS_OK, 200)
        self.assertEqual(health.STATUS_NOT_FOUND, 404)
        self.assertEqual(health.STATUS_METHOD_NOT_ALLOWED, 405)

    def test_error_bodies_are_compact_single_member_json(self):
        """Both error bodies parse, carry one ``error`` member, and are compact."""
        for body, expected in (
            (health.NOT_FOUND_BODY, "Not Found"),
            (health.METHOD_NOT_ALLOWED_BODY, "Method Not Allowed"),
        ):
            with self.subTest(expected=expected):
                self.assertIsInstance(body, bytes)
                text = body.decode(EXPECTED_ENCODING)
                self.assertNotIn(", ", text)
                self.assertNotIn(": ", text)
                document = json.loads(text)
                self.assertEqual(document, {"error": expected})

    def test_compiled_in_fallbacks_declare_this_tier(self):
        """The last link of the precedence chain carries this tier's real values.

        These literals are what keep the endpoint serving a valid contract when
        a configuration file is missing from a container image -- exactly the
        failure mode a health endpoint has to survive.
        """
        self.assertEqual(health.FALLBACK_NAME, EXPECTED_NAME)
        self.assertEqual(health.FALLBACK_VERSION, EXPECTED_VERSION)
        self.assertEqual(health.FALLBACK_STATUS, EXPECTED_STATUS)
        self.assertEqual(health.FALLBACK_PATH, EXPECTED_PATH)
        self.assertEqual(health.FALLBACK_HOST, EXPECTED_BIND_HOST)
        self.assertEqual(health.FALLBACK_PORT, TIER_PORT)

    def test_environment_override_names_are_this_tiers_prefixed_pair(self):
        """Level 2 overrides are ``HEALTH_HOST``/``HEALTH_PORT``, never bare names.

        Only Level 1 uses bare ``HOST``/``PORT``.  The asymmetry is deliberate,
        so that all three tiers can run side by side on one host during
        validation without one tier's environment reconfiguring another's.
        """
        self.assertEqual(health.ENV_HOST, "HEALTH_HOST")
        self.assertEqual(health.ENV_PORT, "HEALTH_PORT")

    def test_resolved_identity_and_path_match_the_declared_values(self):
        """Resolution produced this tier's identity, its path and its status.

        ``HOST`` and ``PORT`` are asserted structurally rather than by value,
        because both accept an environment override and a validation host is
        entitled to set one; the *declared* values are pinned by
        :meth:`test_compiled_in_fallbacks_declare_this_tier` and by the
        configuration-source tests instead.
        """
        self.assertEqual(health.APP_NAME, EXPECTED_NAME)
        self.assertEqual(health.APP_VERSION, EXPECTED_VERSION)
        self.assertEqual(health.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(health.STATUS, EXPECTED_STATUS)
        self.assertIsInstance(health.HOST, str)
        self.assertTrue(health.HOST)
        self.assertIsInstance(health.PORT, int)
        self.assertFalse(isinstance(health.PORT, bool))
        self.assertGreaterEqual(health.PORT, 0)
        self.assertLessEqual(health.PORT, 65535)

    def test_get_config_returns_an_independent_snapshot(self):
        """Each call returns a fresh mapping, so a caller cannot mutate the module."""
        first = health.get_config()
        second = health.get_config()
        self.assertEqual(
            sorted(first), ["host", "name", "path", "port", "status", "version"]
        )
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["name"] = "mutated"
        self.assertEqual(health.get_config()["name"], EXPECTED_NAME)
        self.assertEqual(health.APP_NAME, EXPECTED_NAME)

    def test_handler_aliases_resolve_to_one_class(self):
        """All three exported spellings are the same handler class.

        The aliases exist so that a consumer importing under either common name
        resolves the same object instead of failing at import time; asserting
        identity keeps them from drifting into three separate implementations.
        """
        self.assertIs(health.HealthHandler, health.HealthRequestHandler)
        self.assertIs(health.HealthCheckHandler, health.HealthRequestHandler)
        self.assertTrue(issubclass(health.HealthRequestHandler, BaseHTTPRequestHandler))
        self.assertEqual(health.HealthRequestHandler.health_path, EXPECTED_PATH)

    def test_the_forbidden_member_list_cannot_reject_a_contract_member(self):
        """A self-check on this file: the two member lists must not intersect.

        Without it, a typo in :data:`FORBIDDEN_MEMBERS` -- adding ``"status"``,
        say -- would make every payload assertion fail for a reason that has
        nothing to do with the implementation.
        """
        self.assertEqual(set(FORBIDDEN_MEMBERS) & set(EXPECTED_MEMBERS), set())
        self.assertEqual(len(set(FORBIDDEN_MEMBERS)), len(FORBIDDEN_MEMBERS))
        self.assertEqual(len(set(EXPECTED_MEMBERS)), len(EXPECTED_MEMBERS))


class TestDeclaredConfigurationSources(unittest.TestCase):
    """Every payload value traces to a declared source, not to an inline literal.

    Every value in the response traces to a declared file: ``name`` and
    ``version`` come from ``pyproject.toml``, the serving parameters come from
    ``config/health.json``, and this class reads both files and asserts that the
    resolved values agree with them.  It is what proves the endpoint is
    configured rather than hardcoded.

    Every access here is read-only.  Nothing in this suite writes to, moves or
    renames a repository file.
    """

    def test_pyproject_is_the_declared_identity_source(self):
        """``[project] name``/``version`` exist, are correct, and were adopted."""
        self.assertTrue(
            health.PYPROJECT_PATH.is_file(), f"{health.PYPROJECT_PATH} must exist"
        )
        with health.PYPROJECT_PATH.open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertEqual(project["name"], EXPECTED_NAME)
        self.assertEqual(project["version"], EXPECTED_VERSION)
        # The resolved values were read from this file, not spelled inline.
        self.assertEqual(health.APP_NAME, project["name"])
        self.assertEqual(health.APP_VERSION, project["version"])
        # ``tomllib`` entered the standard library in CPython 3.11, which is the
        # floor this manifest has to declare for the read above to be possible.
        self.assertIn("requires-python", project)
        self.assertEqual(project["requires-python"], ">=3.11")

    def test_config_health_json_is_the_declared_serving_source(self):
        """The serving document declares this tier's host, port, path and status."""
        self.assertTrue(
            health.CONFIG_PATH.is_file(), f"{health.CONFIG_PATH} must exist"
        )
        document = json.loads(health.CONFIG_PATH.read_text(encoding=EXPECTED_ENCODING))
        self.assertEqual(document["host"], EXPECTED_BIND_HOST)
        self.assertEqual(document["port"], TIER_PORT)
        self.assertEqual(document["path"], EXPECTED_PATH)
        self.assertEqual(document["status"], EXPECTED_STATUS)
        # Neither the path nor the status has an environment override, so the
        # resolved value must equal the configured one exactly.
        self.assertEqual(health.HEALTH_PATH, document["path"])
        self.assertEqual(health.STATUS, document["status"])

    def test_sources_are_resolved_from_the_module_location_not_the_cwd(self):
        """Both declared sources sit beside the module that reads them.

        Resolving from ``__file__`` rather than the process's working directory
        is what makes the values identical whether the server is launched from
        the repository root, from a parent directory, or as ``/app/server.py``
        inside the container image.
        """
        self.assertEqual(
            health.MODULE_DIR, pathlib.Path(health.__file__).resolve().parent
        )
        self.assertEqual(health.PYPROJECT_PATH.parent, health.MODULE_DIR)
        self.assertEqual(health.CONFIG_PATH.parent.parent, health.MODULE_DIR)
        self.assertEqual(health.PYPROJECT_PATH.name, "pyproject.toml")
        self.assertEqual(health.CONFIG_PATH.name, "health.json")

    def test_the_payload_carries_the_configured_values(self):
        """A built payload agrees with both declared sources, member by member."""
        with health.PYPROJECT_PATH.open("rb") as handle:
            project = tomllib.load(handle)["project"]
        document = json.loads(health.CONFIG_PATH.read_text(encoding=EXPECTED_ENCODING))
        payload = health.build_payload()
        self.assertEqual(payload["name"], project["name"])
        self.assertEqual(payload["version"], project["version"])
        self.assertEqual(payload["status"], document["status"])


class TestHealthPayload(PayloadContractAssertions, unittest.TestCase):
    """The payload contract, asserted with no socket in play.

    ``health.py`` keeps the payload builder separate from the listener precisely
    so this is possible: the body's shape, its member order, its values and its
    per-request freshness are all provable without binding a port, which makes
    these the fastest and least flaky assertions in the suite.
    """

    def test_payload_conforms_to_the_frozen_contract(self):
        """One call satisfies every clause of the body contract."""
        self.assert_payload_conforms(health.build_payload())

    def test_payload_has_exactly_four_members_in_the_frozen_order(self):
        """Order asserted as a list, and the count asserted alongside it.

        A set comparison would pass for any permutation, and permutation is
        exactly what a careless ``sort_keys=True`` would introduce.
        """
        payload = health.build_payload()
        self.assertEqual(list(payload.keys()), EXPECTED_MEMBERS)
        self.assertEqual(len(payload), 4)

    def test_payload_carries_no_undeclared_member(self):
        """No fifth member, checked name by name for an actionable failure."""
        payload = health.build_payload()
        for forbidden in FORBIDDEN_MEMBERS:
            with self.subTest(member=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_name_is_this_tiers_identifier(self):
        """``name`` identifies the tier serving the request, not the composition."""
        self.assertEqual(health.build_payload()["name"], EXPECTED_NAME)

    def test_version_is_the_declared_semver_string(self):
        """``version`` is the cross-tier consistency value, and it is text."""
        version = health.build_payload()["version"]
        self.assertEqual(version, EXPECTED_VERSION)
        self.assertIsInstance(version, str)

    def test_status_is_the_literal_up(self):
        """``status`` is the literal ``UP`` -- uppercase, exactly two characters."""
        self.assertEqual(health.build_payload()["status"], EXPECTED_STATUS)

    def test_every_member_is_a_string(self):
        """All four values are JSON strings, so a consumer needs no type sniffing."""
        payload = health.build_payload()
        for member, value in payload.items():
            with self.subTest(member=member):
                self.assertIsInstance(value, str)

    def test_timestamp_has_millisecond_precision_and_a_z_suffix(self):
        """The timestamp matches the contract pattern exactly."""
        self.assert_timestamp_conforms(health.build_payload()["timestamp"])

    def test_timestamp_is_not_isoformat_shaped(self):
        """The two ``isoformat()`` artefacts are absent: ``+00:00`` and six digits.

        Asserted separately from the pattern match because this is the single
        most likely way for the timestamp to regress, and a named assertion says
        so in the failure output.
        """
        timestamp = health.build_payload()["timestamp"]
        self.assertNotIn("+00:00", timestamp)
        self.assertNotIn("+", timestamp)
        self.assertEqual(timestamp.count("."), 1)
        self.assertEqual(len(timestamp), len("YYYY-MM-DDTHH:MM:SS.mmmZ"))

    def test_current_timestamp_helper_matches_the_same_pattern(self):
        """The exported formatter conforms on its own, not only inside a payload."""
        self.assert_timestamp_conforms(health.current_timestamp())

    def test_timestamp_is_generated_on_every_call(self):
        """Two builds differ in ``timestamp`` and agree on everything else.

        This is what makes the endpoint proof of *liveness* rather than proof of
        reachability: a process frozen after binding its socket cannot keep
        serving a stale but well-formed payload and be called healthy.  The pause
        is 20 ms -- an order of magnitude more than the millisecond resolution
        needs -- so the assertion is deterministic without slowing the suite.

        The comparison is ordering as well as inequality: these timestamps are
        fixed-width UTC strings, so lexicographic order is chronological order,
        and the second value must sort after the first.
        """
        first = health.build_payload()
        time.sleep(FRESHNESS_PAUSE_SECONDS)
        second = health.build_payload()
        self.assertNotEqual(first["timestamp"], second["timestamp"])
        self.assertGreater(second["timestamp"], first["timestamp"])
        for member in ("name", "version", "status"):
            with self.subTest(member=member):
                self.assertEqual(first[member], second[member])

    def test_each_call_returns_an_independent_mapping(self):
        """A caller mutating one payload cannot affect the next one built.

        Proves the builder constructs a fresh object per call rather than handing
        out a shared, cached dictionary -- which is also what guarantees the
        timestamp is never memoized.
        """
        first = health.build_payload()
        second = health.build_payload()
        self.assertIsNot(first, second)
        first["status"] = "DOWN"
        first["injected"] = "value"
        third = health.build_payload()
        self.assertEqual(third["status"], EXPECTED_STATUS)
        self.assertEqual(list(third), EXPECTED_MEMBERS)
        self.assertEqual(second["status"], EXPECTED_STATUS)


class TestHealthPayloadSerialization(PayloadContractAssertions, unittest.TestCase):
    """The byte-level clauses of the contract.

    The three tiers are required to be byte-shape identical for identical field
    values, and the one thing that can silently break that in Python is
    ``json.dumps``'s default separators.  These tests pin the compact form, the
    exact byte shape, the encoding and the member order, and one of them
    demonstrates the divergence the compact separators exist to prevent.
    """

    def test_serialize_emits_compact_utf8_bytes(self):
        """The serializer returns UTF-8 ``bytes`` with no insignificant whitespace."""
        rendered = health.serialize(health.build_payload())
        self.assertIsInstance(rendered, bytes)
        self.assert_serialization_is_compact(rendered.decode(EXPECTED_ENCODING))

    def test_serialize_matches_an_explicit_compact_dump(self):
        """The serializer is exactly ``json.dumps(..., separators=(",", ":"))``.

        Compared against an independently written compact dump of the *same*
        payload object, so the comparison is exact rather than shape-based and no
        timestamp value is ever asserted.
        """
        payload = health.build_payload()
        expected = json.dumps(payload, separators=(",", ":")).encode(EXPECTED_ENCODING)
        self.assertEqual(health.serialize(payload), expected)

    def test_default_separators_would_diverge(self):
        """Default separators produce different bytes for identical data.

        This is the measured risk the contract's ``separators`` argument exists
        to eliminate: Python's default ``", "``/``": "`` output parses to the same
        document but is not the same byte sequence as the JavaScript tier's
        compact ``JSON.stringify``.  Asserting the divergence keeps the reason
        for the argument visible, so it cannot be "simplified away" later.
        """
        payload = health.build_payload()
        compact = json.dumps(payload, separators=(",", ":"))
        default = json.dumps(payload)
        self.assertNotEqual(compact, default)
        self.assertLess(len(compact), len(default))
        self.assertIn(", ", default)
        self.assertIn(": ", default)
        self.assert_serialization_is_compact(compact)
        # Different bytes, identical data: the divergence is purely in the shape.
        self.assertEqual(json.loads(compact), json.loads(default))

    def test_serialized_body_has_the_expected_byte_shape(self):
        """The body is pinned around the one member that varies."""
        self.assert_byte_shape(health.render_payload().decode(EXPECTED_ENCODING))

    def test_round_trip_preserves_member_order_and_count(self):
        """Parsing the serialized body yields the four members, still in order."""
        payload = health.build_payload()
        parsed = json.loads(health.serialize(payload).decode(EXPECTED_ENCODING))
        self.assertEqual(list(parsed.keys()), EXPECTED_MEMBERS)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed, payload)
        self.assert_payload_conforms(parsed)

    def test_render_payload_serializes_a_freshly_built_payload(self):
        """``render_payload`` is ``serialize(build_payload())``, fresh each call."""
        first = health.render_payload()
        time.sleep(FRESHNESS_PAUSE_SECONDS)
        second = health.render_payload()
        self.assertIsInstance(first, bytes)
        self.assertNotEqual(first, second)
        for rendered in (first, second):
            with self.subTest(rendered=rendered):
                self.assert_byte_shape(rendered.decode(EXPECTED_ENCODING))
                self.assert_payload_conforms(
                    json.loads(rendered.decode(EXPECTED_ENCODING))
                )

    def test_serialized_length_is_stable_across_calls(self):
        """Successive bodies have identical length: the timestamp is fixed-width.

        A stable ``Content-Length`` is what lets the handler advertise an
        accurate length on every response, including a ``HEAD``.
        """
        self.assertEqual(len(health.render_payload()), len(health.render_payload()))


class TestHealthPayloadFallbackResilience(PayloadContractAssertions, unittest.TestCase):
    """The endpoint still answers correctly when its configuration files are gone.

    An endpoint that cannot report its own health because its own configuration
    is missing is worse than no endpoint at all, so the compiled-in literals are
    the last link of the precedence chain and this class proves they work.

    The technique matters as much as the assertion.  A copy of ``health.py`` is
    executed inside an empty temporary directory, so the module's
    location-derived paths point at files that genuinely do not exist.  Nothing
    in the repository is deleted, renamed, moved or written; the temporary
    directory is removed by a registered cleanup; the copy is never inserted into
    ``sys.modules``; and :meth:`tearDown` proves the two real declared sources
    are byte-for-byte unchanged afterwards.
    """

    def setUp(self):
        """Snapshot the real declared sources and open a private temporary workspace."""
        # Captured before anything else so the comparison in tearDown is honest
        # even if the body of a test fails part-way through.
        self._identity_bytes = health.PYPROJECT_PATH.read_bytes()
        self._serving_bytes = health.CONFIG_PATH.read_bytes()
        workspace = tempfile.TemporaryDirectory(prefix="blitzy_health_fallback_")
        self.addCleanup(workspace.cleanup)
        self._workspace = pathlib.Path(workspace.name).resolve()

    def tearDown(self):
        """Prove the repository's two declared sources are byte-for-byte unchanged."""
        self.assertEqual(
            health.PYPROJECT_PATH.read_bytes(),
            self._identity_bytes,
            "the real pyproject.toml must be untouched by this suite",
        )
        self.assertEqual(
            health.CONFIG_PATH.read_bytes(),
            self._serving_bytes,
            "the real config/health.json must be untouched by this suite",
        )

    def _load_isolated_module(self):
        """Execute a copy of ``health.py`` from an otherwise empty directory."""
        copy = self._workspace / pathlib.Path(health.__file__).name
        copy.write_bytes(pathlib.Path(health.__file__).resolve().read_bytes())
        module, stdout_text, stderr_text = _execute_module_source(
            _HEALTH_PROBE_MODULE_NAME, copy
        )
        # Importing the module must be silent: start-up logging belongs to the
        # entry point, never to an imported module.
        self.assertEqual(stdout_text, "")
        self.assertEqual(stderr_text, "")
        self.assertEqual(module.MODULE_DIR, self._workspace)
        self.assertFalse(
            module.PYPROJECT_PATH.exists(),
            "the fallback test is only meaningful with the identity source absent",
        )
        self.assertFalse(
            module.CONFIG_PATH.exists(),
            "the fallback test is only meaningful with the serving source absent",
        )
        return module

    def test_payload_still_conforms_with_both_sources_absent(self):
        """No configuration at all, and the four-member contract still holds."""
        module = self._load_isolated_module()
        payload = module.build_payload()
        self.assert_payload_conforms(payload)
        self.assert_byte_shape(module.serialize(payload).decode(EXPECTED_ENCODING))

    def test_identity_and_status_fall_back_to_the_compiled_in_literals(self):
        """``name``, ``version``, ``path`` and ``status`` come from the literals.

        Neither the identity pair nor the path nor the status has an environment
        override, so with both files absent these four values are provably the
        compiled-in fallbacks.  ``HOST`` and ``PORT`` are excluded on purpose:
        both accept an override, so asserting them by value here would make the
        test depend on the environment it happens to run in.
        """
        module = self._load_isolated_module()
        self.assertEqual(module.APP_NAME, module.FALLBACK_NAME)
        self.assertEqual(module.APP_VERSION, module.FALLBACK_VERSION)
        self.assertEqual(module.HEALTH_PATH, module.FALLBACK_PATH)
        self.assertEqual(module.STATUS, module.FALLBACK_STATUS)
        self.assertEqual(module.APP_NAME, EXPECTED_NAME)
        self.assertEqual(module.APP_VERSION, EXPECTED_VERSION)
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(module.STATUS, EXPECTED_STATUS)

    def test_the_real_module_is_unaffected_by_the_isolated_copy(self):
        """Loading a copy leaves the imported ``health`` module exactly as it was."""
        module = self._load_isolated_module()
        self.assertIsNot(module, health)
        self.assertNotEqual(module.MODULE_DIR, health.MODULE_DIR)
        self.assertEqual(
            health.MODULE_DIR, pathlib.Path(health.__file__).resolve().parent
        )
        self.assertTrue(health.PYPROJECT_PATH.is_file())
        self.assertTrue(health.CONFIG_PATH.is_file())
        self.assert_payload_conforms(health.build_payload())


class TestHealthEndpointOverHttp(PayloadContractAssertions, unittest.TestCase):
    """The contract asserted over real HTTP, against a real listener.

    Everything about how this listener is run is a safety requirement rather
    than a convenience:

    * **``("127.0.0.1", 0)``.**  Port 0 makes the operating system assign a free
      ephemeral port, which is read back from ``server_address``; the tier's
      declared port 8000 is never bound, so this suite passes whether or not the
      real server is running and can never collide with it or with a developer's
      session.  ``127.0.0.1`` rather than ``0.0.0.0`` keeps the socket off every
      other interface -- a test must not publish a service.
    * **A daemon thread.**  The listener cannot outlive the interpreter even if
      teardown were somehow skipped.
    * **Guaranteed teardown.**  ``shutdown()`` then ``server_close()``, in a
      ``finally``, with the thread joined and its death asserted.  A leaked
      listener or a hung suite would block CI indefinitely, so this class fails
      loudly rather than leaving either behind.
    * **Polled readiness and bounded requests.**  Readiness is established by
      probing rather than by a blind sleep, and every request carries a timeout,
      so a defect surfaces as a failure instead of a hang.

    ``server.py`` is deliberately neither imported nor started: it binds port
    8000 by design.  The listener here is assembled from the handler directly,
    which is the whole reason ``health.py`` never binds a socket of its own.
    """

    _server = None
    _thread = None
    _host = None
    _port = None

    @classmethod
    def setUpClass(cls):
        """Bind an ephemeral loopback listener, serve it, and wait until it answers."""
        cls._server = ThreadingHTTPServer(
            (LOOPBACK_HOST, EPHEMERAL_PORT), health.HealthRequestHandler
        )
        cls._host, cls._port = cls._server.server_address[:2]
        cls._thread = threading.Thread(
            target=cls._server.serve_forever,
            kwargs={"poll_interval": LISTENER_POLL_INTERVAL_SECONDS},
            name="blitzy-health-contract-listener",
            daemon=True,
        )
        cls._thread.start()
        try:
            cls._await_readiness()
        except BaseException:
            # The socket is already bound at this point, so it has to be
            # released before the failure propagates.
            cls._stop_listener()
            raise

    @classmethod
    def tearDownClass(cls):
        """Release the listener and fail loudly if its thread outlived shutdown."""
        thread = cls._thread
        cls._stop_listener()
        if thread is not None and thread.is_alive():
            raise AssertionError(
                "the test listener thread outlived shutdown(); a leaked listener "
                "would block a subsequent run"
            )

    @classmethod
    def _stop_listener(cls):
        """Release the listener unconditionally, then let the thread finish."""
        server, thread = cls._server, cls._thread
        cls._server, cls._thread = None, None
        try:
            if server is not None:
                server.shutdown()
        finally:
            if server is not None:
                server.server_close()
            if thread is not None:
                thread.join(LISTENER_JOIN_TIMEOUT_SECONDS)

    @classmethod
    def _await_readiness(cls):
        """Poll the endpoint until it answers, or fail with a clear diagnosis."""
        for _ in range(READINESS_ATTEMPTS):
            try:
                response = cls._perform("GET", EXPECTED_PATH)
            except (OSError, http.client.HTTPException):
                time.sleep(READINESS_PAUSE_SECONDS)
                continue
            if response.status == health.STATUS_OK:
                return
            raise AssertionError(
                f"the listener answered {response.status} during readiness "
                f"polling; {health.STATUS_OK} was required"
            )
        raise AssertionError(
            "the listener did not become ready within "
            f"{READINESS_ATTEMPTS * READINESS_PAUSE_SECONDS:.2f}s"
        )

    @classmethod
    def _perform(cls, method, target, body=None):
        """Perform one bounded request and return an :class:`_Response` snapshot.

        The connection is always closed, which also releases the handler thread
        immediately instead of leaving it waiting out its idle timeout on a
        persistent connection.
        """
        connection = http.client.HTTPConnection(
            cls._host, cls._port, timeout=REQUEST_TIMEOUT_SECONDS
        )
        try:
            connection.request(method, target, body=body)
            response = connection.getresponse()
            return _Response(response.status, response.headers, response.read())
        finally:
            connection.close()

    def _assert_contract_headers(self, response):
        """Assert the headers every response carries, and an accurate length."""
        self.assertEqual(response.header("Content-Type"), EXPECTED_CONTENT_TYPE)
        self.assertEqual(response.header("Cache-Control"), EXPECTED_CACHE_CONTROL)
        self.assertEqual(response.header("Content-Length"), str(len(response.body)))

    # -- The listener itself -----------------------------------------------

    def test_listener_is_loopback_only_and_never_the_tier_port(self):
        """A self-guard: this suite binds an ephemeral loopback port, not 8000.

        Asserted as a test rather than left to review, because binding the tier's
        real port is the one mistake in this file that would break CI for a
        reason unrelated to the code under test.
        """
        self.assertEqual(self._host, LOOPBACK_HOST)
        self.assertNotEqual(self._port, TIER_PORT)
        self.assertNotEqual(self._port, EPHEMERAL_PORT)
        self.assertGreater(self._port, 0)
        self.assertLessEqual(self._port, 65535)
        self.assertEqual(self._server.server_address[0], LOOPBACK_HOST)

    # -- Success paths -----------------------------------------------------

    def test_get_health_returns_the_full_contract(self):
        """``GET /health`` answers 200 with the contract's headers and body."""
        response = self._perform("GET", EXPECTED_PATH)
        self.assertEqual(response.status, 200)
        self._assert_contract_headers(response)
        self.assertIsNone(
            response.header("Allow"), "Allow belongs only on a 405 response"
        )
        self.assert_byte_shape(response.text())
        self.assert_payload_conforms(response.json())

    def test_header_field_names_are_case_insensitive(self):
        """The headers are retrievable under any casing, as HTTP requires.

        Recorded as an assertion because the sibling Java tier emits
        ``Content-type`` and ``Cache-control``; every probe of this contract --
        here, in the container ``HEALTHCHECK`` and in CI -- must therefore match
        field names case-insensitively rather than by exact string.
        """
        response = self._perform("GET", EXPECTED_PATH)
        self.assertEqual(response.header("content-type"), EXPECTED_CONTENT_TYPE)
        self.assertEqual(response.header("CACHE-CONTROL"), EXPECTED_CACHE_CONTROL)

    def test_query_string_is_ignored(self):
        """``GET /health?x=1`` is the same resource: the query string is tolerated."""
        response = self._perform("GET", f"{EXPECTED_PATH}?x=1")
        self.assertEqual(response.status, 200)
        self._assert_contract_headers(response)
        self.assert_payload_conforms(response.json())

    def test_head_returns_the_status_and_headers_with_no_body(self):
        """``HEAD /health`` is a valid, cheap liveness check.

        The status and headers match a ``GET`` exactly and ``Content-Length``
        still reports the length a ``GET`` would have produced -- so the payload
        was really built and measured -- while the body is empty.
        """
        get_response = self._perform("GET", EXPECTED_PATH)
        head_response = self._perform("HEAD", EXPECTED_PATH)
        self.assertEqual(head_response.status, 200)
        self.assertEqual(head_response.body, b"")
        self.assertEqual(head_response.header("Content-Type"), EXPECTED_CONTENT_TYPE)
        self.assertEqual(head_response.header("Cache-Control"), EXPECTED_CACHE_CONTROL)
        self.assertEqual(
            head_response.header("Content-Length"), str(len(get_response.body))
        )

    def test_the_response_does_not_disclose_the_runtime(self):
        """A liveness probe advertises no interpreter version.

        Not part of the response contract, which is exactly why it is worth
        asserting: the ``Server`` header is emitted by the standard library and
        would otherwise carry ``Python/3.x.y`` to any unauthenticated caller.
        """
        server_header = self._perform("GET", EXPECTED_PATH).header("Server") or ""
        self.assertNotIn("Python", server_header)
        self.assertNotIn("BaseHTTP", server_header)

    # -- Negative paths ----------------------------------------------------

    def test_post_is_rejected_with_the_allow_header(self):
        """``POST /health`` answers 405 and names the methods that are permitted."""
        response = self._perform("POST", EXPECTED_PATH)
        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)
        self._assert_contract_headers(response)
        self.assertEqual(response.json(), {"error": "Method Not Allowed"})
        self.assert_serialization_is_compact(response.text())

    def test_every_other_method_is_rejected_identically(self):
        """Any method beyond ``GET``/``HEAD`` answers 405 -- never 501.

        A caller therefore always learns the most actionable fact first: that its
        method is not permitted anywhere on this server.
        """
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                response = self._perform(method, EXPECTED_PATH)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)
                self.assertEqual(response.json(), {"error": "Method Not Allowed"})

    def test_method_is_checked_before_path(self):
        """``POST /unknown`` is a 405, not a 404: the method is rejected first."""
        response = self._perform("POST", "/unknown")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)

    def test_unknown_path_returns_a_small_json_error_body(self):
        """``GET /unknown`` answers 404 with a body that parses as JSON."""
        response = self._perform("GET", "/unknown")
        self.assertEqual(response.status, 404)
        self._assert_contract_headers(response)
        self.assertEqual(response.json(), {"error": "Not Found"})
        self.assertLess(len(response.body), 64, "the error body must stay small")
        self.assert_serialization_is_compact(response.text())

    def test_a_trailing_slash_is_not_an_alias_for_the_endpoint(self):
        """``GET /health/`` answers 404: the path comparison is exact.

        The contract admits exactly one route.  Nothing is normalized, so no
        undocumented alias exists for the single resource this server serves.
        """
        response = self._perform("GET", f"{EXPECTED_PATH}/")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json(), {"error": "Not Found"})

    # -- Freshness ---------------------------------------------------------

    def test_consecutive_requests_differ_only_in_the_timestamp(self):
        """Two probes 20 ms apart: a new timestamp, an identical identity.

        This is the wire-level form of the freshness clause, and it is what
        distinguishes a live process from a merely reachable one.
        """
        first = self._perform("GET", EXPECTED_PATH).json()
        time.sleep(FRESHNESS_PAUSE_SECONDS)
        second = self._perform("GET", EXPECTED_PATH).json()
        self.assert_payload_conforms(first)
        self.assert_payload_conforms(second)
        self.assertNotEqual(first["timestamp"], second["timestamp"])
        self.assertGreater(second["timestamp"], first["timestamp"])
        for member in ("name", "version", "status"):
            with self.subTest(member=member):
                self.assertEqual(first[member], second[member])

    def test_the_served_payload_matches_the_in_process_builder(self):
        """The wire and the builder agree on every stable member.

        Proves the handler serves the same payload the socket-free assertions
        cover, so the two halves of this suite are testing one implementation
        rather than two.
        """
        served = self._perform("GET", EXPECTED_PATH).json()
        built = health.build_payload()
        self.assertEqual(list(served), list(built))
        for member in ("name", "version", "status"):
            with self.subTest(member=member):
                self.assertEqual(served[member], built[member])


if __name__ == "__main__":
    # Direct execution mirrors what `python -m unittest` does through discovery,
    # so a contributor can run this one file without remembering an incantation.
    unittest.main(verbosity=2)
