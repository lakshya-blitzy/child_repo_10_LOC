"""Standard-library ``unittest`` suite for the Level 2 (``child_repo_10_LOC``) tier.

Five things are asserted, and they carry equal weight.

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
   against a real listener bound to an **ephemeral loopback port**.  The
   contract is reproduced in full below.
3. **The configuration precedence chain, behaviourally.**  ``HEALTH_HOST`` and
   ``HEALTH_PORT`` are not merely named here; a copy of ``health.py`` is executed
   under controlled environment values, from a directory whose configuration
   sources are either purpose-written or absent, and every link of
   ``environment -> configuration file -> compiled-in literal`` is asserted in
   both directions: a usable override wins, and an unusable one falls through.
4. **The process entry point.**  ``server.py`` -- the program a deployment
   actually runs -- is exercised twice over: its pure helpers and its
   listener in this process, and the whole program as a real child process on a
   port this suite reserves and releases first (see
   :func:`_reserve_free_port`), probed over HTTP, stopped with ``SIGTERM`` and
   ``SIGINT``, and failed deliberately against an occupied port to assert its
   diagnostic.
5. **Request framing and parser safety.**  A rejected request that carries a body,
   a chunked body, an unparseable or oversized ``Content-Length``, a truncated
   body, a malformed request line and an over-long target are all sent as raw
   bytes, because none of them can be expressed through a well-behaved client.
   Each one must be answered inside the contract's compact JSON shape, must not
   reflect any request bytes back, and must not desynchronize a persistent
   connection.

The contract, spelled out as the request/response matrix this suite asserts::

    GET|HEAD /health      -> 200, four-member body, in this order:
                             name, version, timestamp, status
    GET      /health?x=1  -> 200, identical (the query string is ignored)
    GET      /health/     -> 404 (the path comparison is exact)
    GET      /%68ealth    -> 404 (the target is never percent-decoded)
    GET      ///health    -> 404 (a run of slashes is never collapsed)
    GET      /./health    -> 404 (a dot segment is never resolved)
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

Two clauses of that contract are asserted here in more depth than a single
example can express, because each is a place where a plausible implementation
silently drifts:

* **``status`` is a validated setting, not a trusted one.**  ``config/health.json``
  is its declared source and the declaration really is read -- so the assertions
  prove the chain is live, not decorative -- but it is adopted only when it is
  exactly ``"UP"``.  Anything else is refused, the compiled-in literal is served
  instead, the provenance published through :func:`health.get_config_sources`
  says ``fallback``, and the refusal is listed by
  :func:`health.frozen_value_conflicts`.  Both directions are therefore
  asserted: a declaration that restates the literal is adopted and reported as
  ``file``, while a tampered isolated copy declaring ``"DOWN"`` cannot move the
  reported status off ``UP``.  ``path`` is resolved the same way.
* **Exactly one spelling of the target is served.**  Nothing is percent-decoded,
  no run of slashes is collapsed and no dot segment is resolved, so every alias
  a URI parser might otherwise fold onto ``/health`` is a ``404``.  The rule is
  asserted twice: directly against :func:`health.request_target_path`, and on
  the wire against a real listener for every spelling in
  :data:`UNSERVED_TARGETS`.

Four deliberate design decisions in this file are worth stating up front,
because each one is load-bearing:

* **Expected values are spelled out as literals here, independently of
  ``health.py``.**  Asserting ``payload["name"] == health.APP_NAME`` would be a
  tautology that passes even if the tier's identity silently changed.  Where a
  test *does* compare against ``health.py``'s own constants, it is asserting
  agreement between two independent sources, never deriving the expectation
  from the code under test.
* **No listener ever binds port 8000.**  8000 is the tier's declared serving
  port, so a real server may be listening on it.  Every listener inside this
  process binds ``("127.0.0.1", 0)`` and reads the assigned port back; every
  ``server.py`` child process is given ``HEALTH_HOST=127.0.0.1`` and a port
  :func:`_reserve_free_port` has just reserved and released, because ``0`` is not
  a usable *configured* value -- ``health.py`` refuses it and would fall through
  to 8000.  Either way this suite passes whether or not the real server is
  running, and never exposes a socket beyond loopback.
* **Every wait is bounded and nothing is left behind.**  Readiness is established
  by polling, never by sleeping; every socket carries a timeout; every child
  process is signalled, awaited under a deadline and killed unconditionally in a
  cleanup; every listener is shut down and closed in a ``finally``; and every
  child interpreter runs with ``PYTHONDONTWRITEBYTECODE`` set, so running this
  suite cannot leave a ``__pycache__`` directory, an orphaned process or a bound
  port behind.
* **Zero third-party packages.**  The repository has none and the target is
  none, so there is no pytest, no plugin, no assertion library, no HTTP client
  library and no clock-freezing library.  ``unittest`` plus the standard library
  is the whole toolchain, and this file is auto-discovered by a bare
  ``python -m unittest`` because it is named ``test_app.py``.

Run it directly with ``python test_app.py``, or through discovery::

    python -m py_compile test_app.py     # static gate
    python -m unittest -v                # discovery: matches test*.py
"""

import contextlib
import errno
import http.client
import importlib.util
import io
import json
import os
import pathlib
import re
import runpy
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app
import health
import server

#: The entry point under its role name.  Some assertions read better when the
#: subject is "the entry point" rather than "the server module".
entry_point = server
from app import greet

# Expected values, spelled independently of the code under test.  A test that
# reads its expectation out of the implementation cannot fail when the
# implementation changes, which makes it documentation rather than a gate.

# The identity is character-exact; the version is a SemVer *string*, never a
# number; the status is exact case.  ``EXPECTED_MEMBERS`` is a ``list`` rather
# than a ``set`` because the order is part of the contract.
EXPECTED_NAME = "child_repo_10_LOC"
EXPECTED_VERSION = "1.0.0"
EXPECTED_STATUS = "UP"
EXPECTED_MEMBERS = ["name", "version", "timestamp", "status"]

# Members that must never appear.  A frozen contract with an extension point is
# a drift point, so a fifth member's absence is asserted explicitly rather than
# left implied by the length check.  Configuration names are here because
# configuration must not leak into the body; the rest are optional members of the
# health-check draft's wider vocabulary that this contract does not adopt.
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
#: the real server.  ``EXPECTED_BIND_HOST`` is asserted as a *configured value*
#: and is never bound by anything in this file; every listener here binds
#: ``LOOPBACK_HOST``.
EXPECTED_PATH = "/health"
EXPECTED_BIND_HOST = "0.0.0.0"

#: Asserted as configuration, and asserted *not* to be the port any listener
#: in this suite binds.
TIER_PORT = 8000

#: A serving document that tries to move the endpoint and invert the status it
#: reports.  ``path`` and ``status`` are validated against the contract literals
#: rather than trusted, so neither of these declarations can be adopted; ``host``
#: and ``port`` are ordinary deployment settings, so both of *those* must be
#: honoured.  Carrying all four in one document is what makes the refusal
#: provable: a test that only asserted the two refusals could pass against an
#: implementation that ignored the file entirely, whereas asserting the two
#: acceptances in the same breath proves the document was read and only the
#: illegal values were refused.
HOSTILE_SERVING_DOCUMENT = {
    "host": "127.0.0.1",
    "port": 8123,
    "path": "/liveness",
    "status": "DOWN",
}

#: Request targets that must *not* reach the endpoint.  Every one of them would
#: resolve to ``/health`` under a normalizing URL parser -- dot-segment
#: collapsing, percent-decoding, or accepting the absolute form of a request
#: target -- and each therefore represents an undocumented alias for the one
#: resource this server serves.  The contract admits exactly one route, so all of
#: them answer ``404``.
#:
#: Grouped by the normalization that would create the alias:
#:
#: * dot segments, literal and percent-encoded -- collapsed by any RFC 3986
#:   ``remove_dot_segments`` implementation, including WHATWG ``URL``;
#: * percent-encoded characters -- a decoding parser maps ``%68`` to ``h`` and
#:   ``%2f`` to ``/``, which is how ``java.net.URI.getPath()`` produces an alias;
#: * the absolute form and the protocol-relative form -- a parser given a base
#:   URL discards the authority and keeps only the path, so a target naming a
#:   foreign host would be served as though it named this one;
#: * two or more leading slashes -- the subtlest of the set. A URI parser reads the
#:   extra slashes as an (empty) authority and hands back the path ``/health``, so a
#:   router that trusted any path accessor -- even an undecoded one -- would serve
#:   them; only a comparison against the transmitted bytes refuses them. CPython's
#:   own ``parse_request`` collapses a run of leading slashes in ``self.path`` for
#:   the same reason, which is why this handler reads the request line instead;
#: * a fragment -- never sent by a conforming client, and not part of the path.
ALIASED_REQUEST_TARGETS = (
    "/other/../health",
    "/./health",
    "/health/../health",
    "/a/b/../../health",
    "/%2e%2e/health",
    "/%2E%2E/health",
    "/%2f%2e%2e%2fhealth",
    "/%68ealth",
    "http://evil.example.com/health",
    "//example.com/health",
    "//health",
    "///health",
    "////health",
    f"{EXPECTED_PATH}#fragment",
)

#: Response headers the contract mandates on a success.
# Contract headers.  ``EXPECTED_ALLOW`` is exact: uppercase, one comma, one
# space, ``GET`` before ``HEAD``.
EXPECTED_CONTENT_TYPE = "application/json; charset=utf-8"
EXPECTED_CACHE_CONTROL = "no-store"
EXPECTED_ALLOW = "GET, HEAD"
EXPECTED_ENCODING = "utf-8"

#: The preserved-behaviour fingerprint of the original program: one space, no
#: punctuation, this capitalization.
GREETED_NAME = "Lakshya"
EXPECTED_GREETING = "Hello Lakshya"

#: Anchored by using ``fullmatch``.  Exactly three fractional digits and a ``Z``
#: suffix, which is what catches the classic ``datetime.isoformat()`` mistake --
#: that emits six fractional digits and a ``+00:00`` offset.
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

#: Every request target that must be served.  A query string is tolerated
#: because the contract says so, and an empty query with it, because both are cut
#: by the same rule.  Nothing else is tolerated -- a fragment is *not* cut, since
#: the query component is the only thing the rule removes.
SERVED_TARGETS = (
    EXPECTED_PATH,
    f"{EXPECTED_PATH}?probe=1",
    f"{EXPECTED_PATH}?",
)

#: Every request target that must **not** be served, each with the specific
#: normalization it proves is absent.  This table is the wire-level form of the
#: "exactly one spelling" clause, and it is deliberately exhaustive: each entry
#: is a spelling that some URI parser -- CPython's, the WHATWG one, or
#: ``java.net.URI`` -- folds onto ``/health``, and every one of them must reach
#: the handler's own JSON ``404`` instead.
UNSERVED_TARGETS = (
    "/%68ealth",           # percent-decoding would serve this
    "/%2Fhealth",          # percent-decoding a slash would not help either
    "///health",           # collapsing a run of slashes would serve this
    "//health",            # BaseHTTPRequestHandler collapses this in `path`
    "//../health",         # collapsing plus dot-segment resolution
    "/./health",           # a dot segment is not resolved
    "/health/../health",   # nor is one in the middle
    "/health%2F",          # an encoded trailing slash is not a trailing slash
    "/health/",            # and a real trailing slash is not the resource
    "/HEALTH",             # the comparison is case-sensitive
    "/healthz",            # a longer path is a different path
    "/unknown",            # the ordinary miss
    "/",                   # the site root is not the endpoint
    "*",                   # the asterisk-form target matches nothing
    "//",                  # neither does a bare authority separator
    "http:/health",        # a scheme with no authority is not absolute-form
    "HTTP:/health",        # ... in any casing
    "http:health",         # nor is an opaque target
    "a:b/health",          # nor an arbitrary scheme
    "//127.0.0.1/health",  # a protocol-relative target is not origin-form
    "/health#fragment",    # a fragment is not cut: only the query component is
    "/health#f?a=1",       # ... and cutting the query leaves the fragment
)

#: The pure rule, asserted as a table: the target as transmitted, and the path
#: it must resolve to (``None`` meaning "matches nothing").  ``PORT`` is
#: substituted with the ephemeral port where a case needs a real authority.
TARGET_PATH_CASES = (
    ("/health", "/health"),
    ("/health?probe=1", "/health"),
    ("/health?", "/health"),
    ("/health#fragment", "/health#fragment"),
    ("/health?a=1#f", "/health"),
    ("/health#f?a=1", "/health#f"),
    ("/%68ealth", "/%68ealth"),
    ("/%2Fhealth", "/%2Fhealth"),
    ("///health", "///health"),
    ("//health", "//health"),
    ("//../health", "//../health"),
    ("/./health", "/./health"),
    ("/health/../health", "/health/../health"),
    ("/health%2F", "/health%2F"),
    ("/health/", "/health/"),
    ("/HEALTH", "/HEALTH"),
    ("/healthz", "/healthz"),
    ("/", "/"),
    ("//", "//"),
    ("http://127.0.0.1:8000/health", None),
    ("http://127.0.0.1:8000/health?probe=1", None),
    ("http://127.0.0.1:8000/%68ealth", None),
    ("http://127.0.0.1:8000", None),
    ("HTTP://127.0.0.1:8000/health", None),
    ("*", None),
    ("http:/health", None),
    ("HTTP:/health", None),
    ("http:health", None),
    ("a:b/health", None),
    ("health", None),
    ("", None),
)

#: Methods that must be refused with ``405``.  ``TRACE``, ``PROPFIND`` and
#: ``MKCOL`` are included because an enumerated ``do_*`` implementation would
#: answer them ``501``, which is not the contract.
REFUSED_METHODS = (
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    "OPTIONS",
    "TRACE",
    "PROPFIND",
    "MKCOL",
)

#: Method tokens no HTTP specification defines.  This tier answers them ``405``
#: like any other refused method -- the interesting part is that it answers at
#: all, since the base class would say ``501`` and the JavaScript tier's parser
#: rejects an unknown token with its own ``400`` before any handler runs.
#: Lowercase ``get`` is included because method names are case-sensitive.
UNRECOGNISED_METHOD_TOKENS = ("FROBNICATE", "get")

#: The value a tampered configuration file declares.  It must never reach the
#: wire: a declared ``status`` is validated against the contract literal, so no
#: configuration file can publish ``DOWN`` from a healthy process.
TAMPERED_STATUS = "DOWN"

#: Serving parameters written into an isolated workspace alongside the tampered
#: status, so the tampering test also proves the file really was read -- a test
#: in which the document is ignored altogether would pass vacuously.
TAMPERED_HOST = "127.0.0.1"
TAMPERED_PORT = 18765
TAMPERED_PATH = "/healthcheck"

#: Loopback only: a test must never publish a socket on another interface.
LOOPBACK_HOST = "127.0.0.1"

#: The operating system's "assign me any free port" sentinel.
#:
#: Usable only where *this suite* binds the listener, by passing it straight to a
#: server constructor.  It is not usable as a configured value: a resolved
#: configuration port of ``0`` is rejected at every tier, because an endpoint on a
#: port the kernel chose cannot be reached by anything configured in advance.
#: Anything that has to reach a *child process*'s listener therefore reserves a
#: concrete number with :func:`_reserve_free_port`.
EPHEMERAL_PORT = 0

#: Poll interval, margin and hard ceiling used by the wall-clock tracking
#: assertion while it waits for real time to pass the last value a burst
#: allocated.  The wait itself is derived from the observed excursion -- that is
#: the point of the assertion -- and the ceiling only guards against a runaway.
#: None of these is ever used to *manufacture* a difference between two
#: timestamps: the contract requires two immediate consecutive calls to differ
#: with no delay at all, so a pause before comparing them would test the clock
#: instead of the endpoint.
CLOCK_CATCH_UP_PAUSE_SECONDS = 0.01
CLOCK_CATCH_UP_MARGIN_SECONDS = 0.2
CLOCK_CATCH_UP_CEILING_SECONDS = 3.0

#: Tolerance allowed between an issued timestamp and the wall clock once a burst
#: has stopped, in milliseconds.
CLOCK_TRACKING_TOLERANCE_MS = 50

#: Timestamps drawn by the sequential-uniqueness assertion.  Several hundred
#: calls in a tight loop is far faster than the millisecond clock ticks, so the
#: whole run sits inside the regime where a raw clock read returns duplicates.
SEQUENTIAL_TIMESTAMP_SAMPLES = 500

#: Threads competing for a timestamp simultaneously, and the samples each draws.
#: ``ThreadingHTTPServer`` dispatches concurrently, so this is the real regime
#: rather than a synthetic one.
CONCURRENT_TIMESTAMP_CALLERS = 8
SAMPLES_PER_CONCURRENT_CALLER = 50

#: Samples drawn before the wall-clock tracking assertion.  Deliberately small:
#: the allocator legitimately runs one millisecond ahead per call inside a single
#: millisecond, and this assertion's subject is that the excursion is bounded and
#: self-cancelling rather than cumulative.
CLOCK_TRACKING_SAMPLES = 40

#: Instants chosen to exercise the formatter deterministically: the epoch, a
#: whole second (where a naive formatter drops the fractional part entirely), a
#: single-digit millisecond that must keep its leading zeros, and the last
#: millisecond of a second.
CRAFTED_INSTANTS_MS = (0, 1_000, 1_769_000_000_000, 1_769_000_000_007, 1_769_000_000_999)

#: Unit conversions used when turning an issued timestamp back into an instant
#: and when reading the wall clock the way the module under test reads it.
MILLISECONDS_PER_SECOND = 1_000
NANOSECONDS_PER_MILLISECOND = 1_000_000

#: Every socket operation is bounded, so a defect surfaces as a test failure
#: rather than a suite that hangs indefinitely.
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

#: Delay before a helper thread asks a *real* listener to stop, so the stop
#: arrives while the listener is genuinely inside ``serve_forever`` rather than
#: before it gets there.  It is a lifecycle pause and nothing else: no assertion
#: in this suite ever pauses to manufacture a difference between two timestamps.
SHUTDOWN_REQUEST_DELAY_SECONDS = 0.05

#: Names under which module *copies* are executed by the helper below.  They are
#: deliberately not ``app`` or ``health``: nothing here may shadow, replace or
#: mutate the real modules.
_APP_PROBE_MODULE_NAME = "blitzy_probe_app_import"
_HEALTH_PROBE_MODULE_NAME = "blitzy_probe_health_fallback"
_SERVER_PROBE_MODULE_NAME = "blitzy_probe_server_import"
_HEALTH_HOSTILE_MODULE_NAME = "blitzy_probe_health_hostile_config"

#: Sentinel asking the degraded-configuration fixture to create a *directory*
#: where a configuration file belongs.  That is how an unopenable source is
#: simulated: revoking read permission is unreliable because an automated
#: environment commonly runs as root, for whom the mode bits do not apply,
#: whereas a directory refuses to be read as a file for every user alike.
_AS_DIRECTORY = object()

#: Serving values written into the *isolated* configuration used by the
#: precedence tests.  They are deliberately unlike both the real configuration
#: and the compiled-in literals, so an assertion can tell which link of the chain
#: produced a value instead of matching two links by coincidence.  Neither is ever
#: bound: the precedence tests resolve configuration and never open a socket.
ISOLATED_CONFIG_HOST = "127.0.0.9"
ISOLATED_CONFIG_PORT = 8123

#: Override values used by the precedence tests, again never bound.
OVERRIDE_HOST = "127.0.0.4"
OVERRIDE_PORT = 8199

#: Upper bound on a ``server.py`` child process announcing its bound address.
#: Binding a loopback socket is immediate, so this can only expire if the entry
#: point failed to start -- which must be reported as a failure with the child's
#: own output attached, never as a hang.
PROCESS_STARTUP_TIMEOUT_SECONDS = 15.0

#: Upper bound on a signalled ``server.py`` child exiting.  ``shutdown`` is
#: driven off the serving thread and ``block_on_close`` is False, so an orderly
#: stop takes milliseconds; this bound exists only to fail safely.
PROCESS_EXIT_TIMEOUT_SECONDS = 10.0

#: Upper bound on waiting for a released port to start refusing connections.
PORT_RELEASE_TIMEOUT_SECONDS = 5.0
PORT_RELEASE_PAUSE_SECONDS = 0.02

#: Ceiling ``health.py`` applies to a declared request body it is willing to
#: drain.  A declared length above it is refused without reading a byte, which is
#: what keeps a bogus ``Content-Length`` from turning into a long blocking read.
MAX_DRAIN_BYTES = 1 << 20

#: Read size for :meth:`_RawConnection.read_until_close`.  Every response this
#: suite reads that way is a few dozen bytes, so one read drains it.
_RAW_READ_CHUNK_BYTES = 1 << 16
_HEALTH_STATUS_PROBE_MODULE_NAME = "blitzy_probe_health_declared_status"


# Helpers.


def _execute_module_source(module_name, source_path):
    """Execute the Python file at *source_path* as a fresh, unregistered module.

    Returns ``(module, stdout_text, stderr_text)``.

    Two properties make this the right tool for both places it is used.  It runs
    the file's top-level code with ``__name__`` set to *module_name* -- never
    ``"__main__"`` -- which is what an ordinary import does, so capturing the
    streams around it proves whether importing the file is silent.  And the
    resulting module is **not** inserted into ``sys.modules``, so the real ``app``
    and ``health`` are never shadowed, replaced or reloaded: no import-order
    dependence and no cross-test state leak.

    Because ``__file__`` is set from *source_path*, a module that derives paths
    from its own location -- as ``health.py`` does -- resolves them relative to
    wherever the copy was placed.  That is what lets a copy in an empty directory
    exercise the configuration-absent fallback without touching a repository file.
    """
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {source_path!r}")
    module = importlib.util.module_from_spec(spec)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        spec.loader.exec_module(module)
    return module, stdout.getvalue(), stderr.getvalue()


@contextlib.contextmanager
def _environment(**overrides):
    """Apply environment *overrides* for the duration of the block, then restore.

    A value of ``None`` **removes** the variable rather than setting it, which is
    the only way to state "this override is genuinely unset" -- distinct from an
    empty string, which is a set-but-blank value that travels a different route
    through the resolver even though it resolves to the same answer.

    Restoration is exact: a variable that was absent before is absent after, and
    one that was present is returned to its original value, on every exit path
    including an exception.  Without that guarantee a single test could silently
    reconfigure every test that ran after it.
    """
    previous = {name: os.environ.get(name) for name in overrides}
    try:
        for name, value in overrides.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _child_environment(**overrides):
    """Build the environment for a child interpreter: inherited, plus *overrides*.

    ``PYTHONDONTWRITEBYTECODE`` is always set so that a child importing ``health``
    or ``server`` cannot materialize a ``__pycache__`` directory inside the
    repository.  The working tree has to be clean after a test run -- that is an
    acceptance criterion, not a preference -- and a test that dirties it to prove
    something else has still failed.

    As in :func:`_environment`, a value of ``None`` removes the variable.
    """
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for name, value in overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def _perform_request(host, port, method, target, body=None, headers=None):
    """Perform one bounded HTTP request and return an :class:`_Response` snapshot.

    The single place in this file where a well-behaved request is made, so every
    caller inherits the same timeout and the same guaranteed close.  Closing the
    connection also releases the handler thread immediately, instead of leaving it
    waiting out its idle timeout on a persistent connection.

    *target* is placed in the request line **verbatim**.  That is the property
    the alias assertions depend on: ``http.client`` neither collapses dot
    segments, nor percent-decodes, nor rewrites an absolute-form target into
    origin form, so a test can send exactly the bytes a hostile client would and
    observe what the handler actually does with them.  A higher-level client that
    normalizes before sending -- ``urllib.request``, ``requests``, JavaScript's
    ``fetch`` -- would quietly rewrite the very input under test and make the
    assertion vacuous.
    """
    connection = http.client.HTTPConnection(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return _Response(response.status, response.headers, response.read())
    finally:
        connection.close()


def _await_port_release(host, port):
    """Return ``True`` once *port* refuses connections, or ``False`` on timeout.

    Asserting that a listener was *released* rather than merely silenced is what
    proves a shutdown was orderly: a port left bound by a half-stopped process is
    exactly what makes the next start-up fail with a puzzling
    "address already in use".  Polling keeps the check both prompt and bounded.
    """
    deadline = time.monotonic() + PORT_RELEASE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        probe = socket.socket()
        probe.settimeout(REQUEST_TIMEOUT_SECONDS)
        try:
            if probe.connect_ex((host, port)) != 0:
                return True
        finally:
            probe.close()
        time.sleep(PORT_RELEASE_PAUSE_SECONDS)
    return False


class _UncloseableReader:
    """Reader proxy whose :meth:`close` is deliberately a no-op.

    ``HTTPResponse.read`` closes the file object it was given once it has consumed
    a body -- unconditionally, not only when the response says the connection ends.
    Handing it the connection's own reader would therefore make reading the *first*
    response destroy the means of reading the second, and a persistent-connection
    test would report a framing defect that does not exist.

    Every other attribute is delegated untouched, so the response still reads
    through the one buffer that holds the connection's bytes.  The real reader is
    closed by :meth:`_RawConnection.close`, which owns it.
    """

    __slots__ = ("_reader",)

    def __init__(self, reader):
        """Remember the reader to delegate to."""
        self._reader = reader

    def __getattr__(self, name):
        """Delegate every attribute except the overridden :meth:`close`."""
        return getattr(self._reader, name)

    def close(self):
        """Ignore the close: the connection, not the response, owns the reader."""


class _SharedReader:
    """Adapter that hands :class:`http.client.HTTPResponse` an existing reader.

    ``HTTPResponse`` calls ``makefile`` on whatever it is given.  Constructing one
    directly on a socket would therefore create a *new* buffered reader per
    response, and the first reader could swallow bytes belonging to the second.
    Sharing one reader across every response on a connection -- wrapped so the
    response cannot close it -- is what makes reading two responses from one socket
    trustworthy.
    """

    __slots__ = ("_reader",)

    def __init__(self, reader):
        """Remember the reader to hand out."""
        self._reader = reader

    def makefile(self, *_args, **_kwargs):
        """Return the shared reader, protected from the response's close."""
        return _UncloseableReader(self._reader)


class _RawConnection:
    """A raw, bounded HTTP/1.1 connection for requests a client cannot express.

    ``http.client`` will not send a malformed request line, an unparseable
    ``Content-Length`` or a body it has not been given, and those are precisely
    the inputs the handler's framing and parser-safety paths exist to survive.  So
    those requests are written as bytes.

    Every operation is bounded by the socket timeout, the reader is shared across
    responses (see :class:`_SharedReader`), and :meth:`close` is idempotent so it
    can be registered as a cleanup unconditionally.
    """

    __slots__ = ("_reader", "_socket")

    def __init__(self, host, port, timeout=REQUEST_TIMEOUT_SECONDS):
        """Open the connection and its shared reader."""
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._reader = self._socket.makefile("rb")

    def send(self, data):
        """Write raw bytes to the peer."""
        self._socket.sendall(data)

    def half_close(self):
        """Shut down the write side, so the peer reads EOF instead of blocking.

        This is how a *truncated* body is expressed: the declared length is never
        satisfied, and the server must notice the peer stopped rather than wait
        out its idle timeout.
        """
        self._socket.shutdown(socket.SHUT_WR)

    def read_response(self, method="GET"):
        """Read one complete response and return an :class:`_Response` snapshot."""
        response = http.client.HTTPResponse(_SharedReader(self._reader), method=method)
        response.begin()
        return _Response(response.status, response.headers, response.read())

    def read_until_close(self):
        """Return every byte the peer sends before it closes, unparsed.

        Needed for the one response shape a client cannot parse: when the
        standard library rejects a request line *before* it has determined the
        request's HTTP version, ``request_version`` is still ``HTTP/0.9``, and
        both ``send_response_only`` and ``send_header`` become no-ops for that
        version -- so the reply is a bare body with no status line and no
        headers.  ``HTTPResponse.begin`` raises ``BadStatusLine`` on that, which
        would hide the property actually worth asserting: that the body is still
        safe, valid, compact JSON that reflects none of the request.
        """
        chunks = []
        while True:
            chunk = self._reader.read(_RAW_READ_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def is_closed_by_peer(self):
        """Return ``True`` when the connection is no longer reusable.

        A read returning no bytes is an orderly EOF.  A reset is not orderly, but
        it means the same thing to a caller asking "may I send another request on
        this socket?" -- and it is a legitimate outcome here, because a server that
        closes while unread request bytes are still in flight provokes one.  Both
        are therefore reported as closed.

        Only a timeout means the connection is genuinely still open with nothing to
        read, and that is the single case reported as ``False``.  It is returned
        rather than raised so a caller can assert either way.
        """
        try:
            return self._reader.read(1) == b""
        except TimeoutError:
            return False
        except OSError:
            return True

    def close(self):
        """Close the reader and the socket, tolerating an already-closed state."""
        with contextlib.suppress(OSError):
            self._reader.close()
        with contextlib.suppress(OSError):
            self._socket.close()


class _ServerProcess:
    """A real ``python server.py`` child process, with bounded waits and cleanup.

    The entry point's three process-level behaviours -- the single announced
    start-up line, the exit status after a signal, and the two-line diagnostic when
    a bind fails -- exist only in a process, so this runs one.  ``server.py``
    exports ``main`` but calls it under a ``__main__`` guard and calls
    ``sys.exit``, so importing it could never demonstrate an exit status.

    Three safety properties are built in rather than left to each caller.  Output
    is drained by a reader thread per stream, so a child can never block writing to
    a full pipe while the test waits for it -- a deadlock that would hang the run.
    Every wait has a deadline and reports what the child printed when it expires,
    so a defect surfaces as a legible failure rather than a hang.  And
    :meth:`stop` escalates to ``SIGKILL`` and is idempotent, so it can be
    registered as a cleanup unconditionally and cannot leave a listener behind.

    The child inherits ``PYTHONDONTWRITEBYTECODE`` from :func:`_child_environment`,
    so running it cannot dirty the working tree.
    """

    #: The start-up line ``server.py`` writes, as a parser.  An IPv6 authority is
    #: bracketed, hence the first alternative.
    _STARTUP_PATTERN = re.compile(
        r"^listening on http://(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<port>\d+)"
        r"(?P<path>/\S*) \((?P<name>\S+) (?P<version>\S+)\)$"
    )

    __slots__ = ("_process", "_readers", "_stderr_lines", "_stdout_lines")

    def __init__(self, arguments=(), **environment_overrides):
        """Spawn ``server.py`` with *arguments* and *environment_overrides* applied.

        *arguments* exists so a test can prove the entry point ignores a command
        line rather than merely assert that no parser is present.
        """
        program = pathlib.Path(server.__file__).resolve()
        self._stdout_lines = []
        self._stderr_lines = []
        # The argv is a fixed list and ``shell`` is left at its default of
        # False, so nothing here is interpreted by a shell.
        self._process = subprocess.Popen(
            [sys.executable, program.name, *arguments],
            cwd=str(program.parent),
            env=_child_environment(**environment_overrides),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=EXPECTED_ENCODING,
        )
        self._readers = [
            self._start_reader(self._process.stdout, self._stdout_lines),
            self._start_reader(self._process.stderr, self._stderr_lines),
        ]

    @staticmethod
    def _start_reader(stream, sink):
        """Drain *stream* into *sink*, one stripped line at a time, on a daemon thread."""

        def _drain():
            """Read until end of file, tolerating a stream closed underneath us."""
            with contextlib.suppress(ValueError, OSError):
                for line in stream:
                    sink.append(line.rstrip("\n"))

        thread = threading.Thread(target=_drain, name="blitzy-server-reader", daemon=True)
        thread.start()
        return thread

    @property
    def stdout_lines(self):
        """Return the lines written to standard output so far."""
        return list(self._stdout_lines)

    @property
    def stderr_lines(self):
        """Return the lines written to standard error so far."""
        return list(self._stderr_lines)

    def _diagnostics(self):
        """Return a description of everything the child printed, for a failure message."""
        return (
            f"exit={self._process.poll()!r}"
            f" stdout={self._stdout_lines!r}"
            f" stderr={self._stderr_lines!r}"
        )

    def await_startup(self):
        """Wait for the announcement and return its parsed fields.

        :returns: a mapping with ``line``, ``host``, ``port``, ``path``, ``name``
            and ``version``.  ``port`` is an ``int`` and is the port the kernel
            actually assigned, and ``host`` is unbracketed so it can be connected
            to directly.
        :raises AssertionError: if the child exits or the deadline passes first.
        """
        deadline = time.monotonic() + PROCESS_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for line in self.stdout_lines:
                matched = self._STARTUP_PATTERN.match(line)
                if matched is not None:
                    fields = matched.groupdict()
                    return {
                        "line": line,
                        "host": fields["host"].strip("[]"),
                        "port": int(fields["port"], 10),
                        "path": fields["path"],
                        "name": fields["name"],
                        "version": fields["version"],
                    }
            if self._process.poll() is not None:
                raise AssertionError(
                    f"server.py exited before announcing an address: {self._diagnostics()}"
                )
            time.sleep(READINESS_PAUSE_SECONDS)
        raise AssertionError(
            f"server.py did not announce an address within"
            f" {PROCESS_STARTUP_TIMEOUT_SECONDS}s: {self._diagnostics()}"
        )

    def send_signal(self, signal_number):
        """Send *signal_number* to the child, tolerating a child that already exited."""
        with contextlib.suppress(ProcessLookupError, OSError):
            self._process.send_signal(signal_number)

    def wait(self):
        """Wait for exit and return the status, with every line of output collected.

        :raises AssertionError: if the child is still running at the deadline.  It
            is killed first, so a failure here still leaves nothing behind.
        """
        try:
            status = self._process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            raise AssertionError(
                f"server.py did not exit within {PROCESS_EXIT_TIMEOUT_SECONDS}s:"
                f" {self._diagnostics()}"
            ) from None
        # Join the readers so the recorded output is complete: the pipes reach end
        # of file when the child exits, so this returns promptly.
        for reader in self._readers:
            reader.join(PROCESS_EXIT_TIMEOUT_SECONDS)
        return status

    def signal_and_wait(self, signal_number):
        """Send *signal_number*, then wait for exit and return the status."""
        self.send_signal(signal_number)
        return self.wait()

    def stop(self):
        """Terminate the child if it is still running.  Idempotent, and never raises."""
        if self._process.poll() is None:
            self.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        for reader in self._readers:
            reader.join(PROCESS_EXIT_TIMEOUT_SECONDS)


class _Response:
    """Immutable snapshot of one HTTP response, safe to use after the close.

    ``http.client`` hands back a live, socket-backed object; every connection in
    this suite is closed in a ``finally`` block, so the status, the headers and
    the fully-read body are copied out first and the tests assert against this
    value object instead.
    """

    #: Named in alphabetical order; the constructor keeps wire order instead.
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


@contextlib.contextmanager
def _patched_attribute(target, name, value):
    """Set ``target.name`` to *value* for the block, then restore it exactly.

    Written out rather than taken from ``unittest.mock`` for the same reason
    everything else in this repository is written out: the composition's
    third-party dependency count is zero, and ``unittest.mock`` is heavier than
    three lines of ``setattr`` need to be.  The restore is in a ``finally``, so a
    failing assertion inside the block cannot leak a substituted attribute into
    the rest of the run -- and the attribute is asserted to exist first, so a
    rename cannot turn this into a silent no-op that invents a new attribute.
    """
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def _reserve_free_port():
    """Reserve a concrete free loopback port number and release it again.

    A configured port of ``0`` is rejected at every tier, so a test that needs a
    *child process* to bind a listener it can then reach must supply a real number.
    Asking the operating system for one and handing it straight back is how that
    number is obtained without hardcoding a guess that could collide with
    something already running on the host.

    The socket is closed before the number is returned, so the caller receives a
    port that is free rather than one this process is holding.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, EPHEMERAL_PORT))
        return probe.getsockname()[1]


@contextlib.contextmanager
def _temporary_listener(handler_class):
    """Serve *handler_class* on an ephemeral loopback port for the block's duration.

    Yields the ``(host, port)`` actually bound.  Port 0 makes the operating system
    assign a free port, so this can never collide with the tier's declared port
    8000 or with a developer's running server; ``127.0.0.1`` keeps the socket off
    every other interface, because a test must not publish a service.

    Readiness is established by polling rather than by a blind sleep, and teardown
    -- ``shutdown()``, ``server_close()``, then joining the thread -- runs in a
    ``finally``, so neither a failing assertion nor a raised exception can leak a
    bound listener into the rest of the run.  The thread is a daemon as a second
    line of defence: it cannot outlive the interpreter even if teardown were
    somehow skipped.
    """
    server = ThreadingHTTPServer((LOOPBACK_HOST, EPHEMERAL_PORT), handler_class)
    host, port = server.server_address[:2]
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": LISTENER_POLL_INTERVAL_SECONDS},
        name="blitzy-health-temporary-listener",
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(READINESS_ATTEMPTS):
            try:
                _perform_request(host, port, "GET", EXPECTED_PATH)
            except (OSError, http.client.HTTPException):
                time.sleep(READINESS_PAUSE_SECONDS)
                continue
            break
        else:
            raise AssertionError(
                "the temporary listener did not become ready within "
                f"{READINESS_ATTEMPTS * READINESS_PAUSE_SECONDS:.2f}s"
            )
        yield host, port
    finally:
        try:
            server.shutdown()
        finally:
            server.server_close()
            thread.join(LISTENER_JOIN_TIMEOUT_SECONDS)


class PayloadContractAssertions:
    """Reusable assertions for the frozen payload, shared by three test classes.

    A mixin rather than a ``TestCase`` subclass on purpose: it defines no
    ``test_*`` method and must never be collected as a test.  It relies on the
    ``assert*`` methods a ``TestCase`` provides, so it is only ever mixed in
    *before* :class:`unittest.TestCase`.

    The same three helpers assert the payload whether it came from
    :func:`health.build_payload` in-process, from an isolated copy of the module
    with no configuration files, or from a real HTTP response -- which is how
    three different execution paths are held to one identical contract.
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
    assertion, and not a formality: a module that does not parse makes this whole
    file uncollectible, so every test below depends on it.
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
        with both streams captured, so ``__name__`` is not ``"__main__"``, the
        guard is false and nothing may be written.  ``greet`` is exercised on the
        freshly executed module too, proving the guard suppresses only the side
        effect and never the definition.
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
        ``sys.modules``, so no subprocess and no ``exec`` is involved.

        This is the only assertion that catches deletion of the *guarded block*
        while ``greet`` survives: the silence test above would still pass, yet
        ``python app.py`` would print nothing and the regression fingerprint
        would be gone.
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

        The contract defines exactly these three and no 5xx, because a routed
        request reads already-resolved values and a clock and so has no failure
        path.  The sibling applications make the same no-5xx-in-the-contract
        promise and each fails closed in the way its own runtime allows -- here,
        by closing the connection without writing anything further.  Whatever any
        of the three emits on that unreachable path is implementation-specific
        and non-normative, so nothing here asserts it.
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
        a configuration file is missing from a deployment -- exactly the failure
        a health endpoint has to survive.
        """
        self.assertEqual(health.FALLBACK_NAME, EXPECTED_NAME)
        self.assertEqual(health.FALLBACK_VERSION, EXPECTED_VERSION)
        self.assertEqual(health.FALLBACK_PATH, EXPECTED_PATH)
        self.assertEqual(health.FALLBACK_HOST, EXPECTED_BIND_HOST)
        self.assertEqual(health.FALLBACK_PORT, TIER_PORT)

    def test_status_is_a_protocol_constant_with_no_resolution_chain(self):
        """``status`` is compiled in, so it has no fallback and no resolved form.

        A fallback is the last link of a precedence chain, and ``status`` has no
        chain to be the last link of: it is the same literal in every
        environment, with the configuration file present, absent or wrong.  The
        absence of a ``FALLBACK_STATUS`` and of a resolved ``STATUS`` is
        therefore asserted directly -- either one reappearing would mean the
        value had been demoted to configuration again.
        """
        self.assertEqual(health.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(len(health.STATUS_UP), 2)
        self.assertEqual(health.STATUS_UP, health.STATUS_UP.upper())
        self.assertFalse(
            hasattr(health, "STATUS"),
            "status must not be resolved from configuration; it is a constant",
        )
        self.assertFalse(
            hasattr(health, "FALLBACK_STATUS"),
            "a constant needs no fallback; a fallback implies a resolution chain",
        )
        self.assertNotIn("STATUS", health.__all__)
        self.assertNotIn("FALLBACK_STATUS", health.__all__)
        self.assertIn("STATUS_UP", health.__all__)

    def test_environment_override_names_are_this_tiers_prefixed_pair(self):
        """Level 2 overrides are ``HEALTH_HOST``/``HEALTH_PORT``, never bare names.

        Only Level 1 uses bare ``HOST``/``PORT``.  The asymmetry is deliberate,
        so that all three tiers can run side by side on one host during
        validation without one tier's environment reconfiguring another's.
        """
        self.assertEqual(health.ENV_HOST, "HEALTH_HOST")
        self.assertEqual(health.ENV_PORT, "HEALTH_PORT")

    def test_resolved_identity_and_path_match_the_declared_values(self):
        """Resolution produced this tier's identity and its path.

        ``HOST`` and ``PORT`` are asserted structurally rather than by value,
        because both accept an environment override and a validation host is
        entitled to set one; the *declared* values are pinned by
        :meth:`test_compiled_in_fallbacks_declare_this_tier` and by the
        configuration-source tests instead.
        """
        self.assertEqual(health.APP_NAME, EXPECTED_NAME)
        self.assertEqual(health.APP_VERSION, EXPECTED_VERSION)
        self.assertEqual(health.HEALTH_PATH, EXPECTED_PATH)
        self.assertIsInstance(health.HOST, str)
        self.assertTrue(health.HOST)
        self.assertIsInstance(health.PORT, int)
        self.assertFalse(isinstance(health.PORT, bool))
        self.assertGreaterEqual(health.PORT, 0)
        self.assertLessEqual(health.PORT, 65535)

    def test_get_config_returns_an_independent_snapshot(self):
        """Each call returns a fresh mapping, so a caller cannot mutate the module.

        The key set is asserted exactly.  Every value the endpoint serves is in it,
        ``status`` included: each one traces to a declared source rather than to a
        literal written inline at its point of use, and the two validated values
        are still unmovable because their declaration is checked against the
        contract before it is adopted.
        """
        first = health.get_config()
        second = health.get_config()
        self.assertEqual(
            sorted(first), ["host", "name", "path", "port", "status", "version"]
        )
        self.assertEqual(first["status"], EXPECTED_STATUS)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["name"] = "mutated"
        first["status"] = "DOWN"
        self.assertEqual(health.get_config()["name"], EXPECTED_NAME)
        self.assertEqual(health.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(health.APP_NAME, EXPECTED_NAME)
        self.assertEqual(health.HEALTH_STATUS, EXPECTED_STATUS)

    def test_get_config_sources_returns_an_independent_snapshot(self):
        """The provenance map is copied per call and keyed exactly as the config.

        A caller must not be able to rewrite the module's own record of where its
        values came from -- that record is the only thing that distinguishes an
        adopted declaration from a refused one when both yield the same value.
        """
        first = health.get_config_sources()
        second = health.get_config_sources()
        self.assertEqual(sorted(first), sorted(health.get_config()))
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertLessEqual(
            set(first.values()),
            {health.FROM_ENVIRONMENT, health.FROM_FILE, health.FROM_FALLBACK},
        )
        first["status"] = "spoofed"
        self.assertEqual(health.get_config_sources()["status"], health.FROM_FILE)

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
        """The serving document declares this tier's host, port and path."""
        self.assertTrue(
            health.CONFIG_PATH.is_file(), f"{health.CONFIG_PATH} must exist"
        )
        document = json.loads(health.CONFIG_PATH.read_text(encoding=EXPECTED_ENCODING))
        self.assertEqual(document["host"], EXPECTED_BIND_HOST)
        self.assertEqual(document["port"], TIER_PORT)
        self.assertEqual(document["path"], EXPECTED_PATH)
        # The declared shape includes ``status``.  It is asserted against
        # ``EXPECTED_STATUS`` -- the literal spelled independently in this file --
        # rather than used to derive an expectation, so a document that drifted
        # would fail here instead of moving the expectation with it.
        self.assertEqual(document["status"], EXPECTED_STATUS)
        # ``path`` and ``status`` are read from this document and then validated
        # against the contract literals, so the endpoint serves ``/health`` and
        # reports ``UP`` whatever the file says (proved by
        # :class:`TestFrozenValuesResistReconfiguration`).  The document declares
        # both on purpose, so that an operator reading this one file sees the whole
        # shape of what is served -- which is why the resolved values and the
        # declared ones agree here, and why that agreement must produce no
        # start-up warning.
        self.assertEqual(health.HEALTH_PATH, document["path"])
        self.assertEqual(health.STATUS_UP, document["status"])
        self.assertEqual(health.frozen_value_conflicts(), ())

    def test_the_declared_status_member_is_read_and_validated(self):
        """The document's ``status`` is read, and adopted only because it is legal.

        ``config/health.json`` declares the literal, because the contract document
        is normative for all three tiers and the declaration keeps the file a
        complete description of the endpoint.  The assertion runs in one direction
        only -- *the file must agree with the constant* -- and never the reverse,
        because the reverse is the defect this test exists to prevent: a payload
        whose status is whatever a configuration file happens to say.

        That the declaration is genuinely *read* rather than decorative is proved
        by the provenance: with this document present, ``status`` resolves from the
        file and no conflict is recorded.  The companion cases in
        :class:`TestStatusIsNotConfigurable` prove the other direction, that an
        illegal declaration resolves to the fallback and is reported.
        """
        document = json.loads(health.CONFIG_PATH.read_text(encoding=EXPECTED_ENCODING))
        self.assertEqual(document["status"], health.STATUS_UP)
        self.assertEqual(document["status"], EXPECTED_STATUS)
        self.assertEqual(health.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(health.get_config_sources()["status"], health.FROM_FILE)
        self.assertEqual(health.frozen_value_conflicts(), ())

    def test_sources_are_resolved_from_the_module_location_not_the_cwd(self):
        """Both declared sources sit beside the module that reads them.

        Resolving from ``__file__`` rather than the process's working directory
        is what makes the values identical wherever the server is launched from.
        """
        self.assertEqual(
            health.MODULE_DIR, pathlib.Path(health.__file__).resolve().parent
        )
        self.assertEqual(health.PYPROJECT_PATH.parent, health.MODULE_DIR)
        self.assertEqual(health.CONFIG_PATH.parent.parent, health.MODULE_DIR)
        self.assertEqual(health.PYPROJECT_PATH.name, "pyproject.toml")
        self.assertEqual(health.CONFIG_PATH.name, "health.json")

    def test_the_payload_carries_the_configured_values(self):
        """A built payload agrees with the declared identity, member by member.

        ``name`` and ``version`` are traced to ``pyproject.toml`` -- they are
        configuration and must not be spelled inline.  ``status`` is traced to
        ``STATUS_UP``, the contract literal a declaration is validated against:
        the declared source may restate it but can never replace it.
        """
        with health.PYPROJECT_PATH.open("rb") as handle:
            project = tomllib.load(handle)["project"]
        payload = health.build_payload()
        self.assertEqual(payload["name"], project["name"])
        self.assertEqual(payload["version"], project["version"])
        self.assertEqual(payload["status"], health.STATUS_UP)


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
        """``status`` is the literal ``UP`` -- uppercase, exactly two characters.

        Compared against both the independent literal spelled in this file and
        the module's own ``STATUS_UP``, so the payload, the contract literal and
        the contract text are pinned to one another in a single assertion.
        """
        self.assertEqual(health.build_payload()["status"], EXPECTED_STATUS)
        self.assertEqual(health.build_payload()["status"], health.STATUS_UP)

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

    def test_crafted_instants_render_with_three_fractional_digits(self):
        """The formatter never drops the fraction, not even on a whole second.

        Rendering chosen instants makes the whole-second case deterministic
        instead of waiting for the clock to land on one.  That case is exactly
        where a naive formatter fails -- it omits the fractional part entirely --
        so a single-shot check against the live clock passes almost always and
        then fails once, unreproducibly, on the run that happens to land there.
        """
        for millis in CRAFTED_INSTANTS_MS:
            with self.subTest(millis=millis):
                rendered = health.format_timestamp(millis)
                self.assert_timestamp_conforms(rendered)
                parsed = datetime.strptime(rendered, "%Y-%m-%dT%H:%M:%S.%f%z")
                self.assertEqual(
                    round(parsed.timestamp() * MILLISECONDS_PER_SECOND), millis
                )
        self.assertTrue(health.format_timestamp(1_000).endswith(".000Z"))
        self.assertTrue(health.format_timestamp(1_769_000_000_007).endswith(".007Z"))

    def test_two_immediate_calls_always_differ(self):
        """Back-to-back calls differ with **no** delay between them.

        This is the assertion the contract's freshness clause actually needs.
        Pausing past a millisecond boundary before comparing would test the clock
        rather than the endpoint, and would pass just as happily against an
        implementation that returns the same value twice whenever two probes
        arrive inside one millisecond -- which is precisely what a raw clock read
        does at this precision.

        The comparison is ordering as well as inequality: these timestamps are
        fixed-width UTC strings, so lexicographic order is chronological order,
        and the second value must sort after the first.
        """
        first = health.current_timestamp()
        second = health.current_timestamp()
        self.assertNotEqual(first, second)
        self.assertGreater(second, first)

    def test_timestamp_is_generated_on_every_call(self):
        """Two immediate builds differ in ``timestamp`` and agree on the rest.

        This is what makes the endpoint proof of *liveness* rather than proof of
        reachability: a process frozen after binding its socket cannot keep
        serving a stale but well-formed payload and be called healthy.  The two
        builds are taken back to back, with nothing between them, so a
        same-millisecond collision would fail the assertion rather than hide
        inside a pause.
        """
        first = health.build_payload()
        second = health.build_payload()
        self.assertNotEqual(first["timestamp"], second["timestamp"])
        self.assertGreater(second["timestamp"], first["timestamp"])
        for member in ("name", "version", "status"):
            with self.subTest(member=member):
                self.assertEqual(first[member], second[member])

    def test_a_long_sequential_run_is_unique_and_ordered(self):
        """Every timestamp in a several-hundred-call run is distinct and later.

        A tight loop of this size completes far faster than the millisecond clock
        ticks, so a raw clock read would return the same value many times over.
        One duplicate here would mean a poller could not distinguish a live
        process from a frozen one.
        """
        drawn = [
            health.current_timestamp() for _ in range(SEQUENTIAL_TIMESTAMP_SAMPLES)
        ]
        self.assertEqual(len(set(drawn)), SEQUENTIAL_TIMESTAMP_SAMPLES)
        self.assertEqual(drawn, sorted(drawn))
        for value in drawn:
            self.assert_timestamp_conforms(value)

    def test_concurrent_callers_never_receive_the_same_timestamp(self):
        """Simultaneous threads each get their own value.

        ``server.py`` serves on ``ThreadingHTTPServer``, so two handlers really do
        ask for a timestamp at the same moment.  A barrier releases every caller
        together to maximize that contention; uniqueness across the collected
        values is what proves the allocation is atomic rather than a
        read-then-write two threads can interleave.
        """
        collected = []
        collected_lock = threading.Lock()
        release = threading.Barrier(CONCURRENT_TIMESTAMP_CALLERS)

        def draw():
            release.wait(timeout=REQUEST_TIMEOUT_SECONDS)
            local = [
                health.current_timestamp()
                for _ in range(SAMPLES_PER_CONCURRENT_CALLER)
            ]
            with collected_lock:
                collected.extend(local)

        callers = [
            threading.Thread(target=draw, name=f"timestamp-caller-{index}")
            for index in range(CONCURRENT_TIMESTAMP_CALLERS)
        ]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=REQUEST_TIMEOUT_SECONDS)
            self.assertFalse(caller.is_alive(), "a caller thread must not hang")

        expected = CONCURRENT_TIMESTAMP_CALLERS * SAMPLES_PER_CONCURRENT_CALLER
        self.assertEqual(len(collected), expected)
        self.assertEqual(len(set(collected)), expected)

    def test_the_timestamp_still_tracks_the_wall_clock(self):
        """A burst stops running ahead as soon as the burst stops.

        Freshness is guaranteed by handing out strictly increasing millisecond
        values, which means a burst of calls inside a single millisecond
        legitimately runs ahead of real time -- by one millisecond per call, and
        no more.  This asserts the other half of that bargain: the excursion is
        bounded and self-cancelling, so once the burst ends the wall clock wins
        again and the endpoint reports the real time rather than an ever-growing
        counter.
        """
        for _ in range(CLOCK_TRACKING_SAMPLES):
            issued_by_burst = health.current_timestamp()

        # The excursion is one millisecond per call at most, so waiting it out
        # takes time proportional to the calls already made -- which is exactly
        # what "bounded" means here.  The wait is therefore derived from the
        # observed gap rather than guessed at, and the ceiling turns a runaway
        # into a failed assertion instead of a hung suite.
        allocated_ms = self._to_epoch_millis(issued_by_burst)
        excursion_seconds = max(
            0, allocated_ms - self._wall_clock_millis()
        ) / MILLISECONDS_PER_SECOND
        budget_seconds = min(
            CLOCK_CATCH_UP_CEILING_SECONDS,
            excursion_seconds + CLOCK_CATCH_UP_MARGIN_SECONDS,
        )

        deadline = time.monotonic() + budget_seconds
        while self._wall_clock_millis() <= allocated_ms and time.monotonic() < deadline:
            time.sleep(CLOCK_CATCH_UP_PAUSE_SECONDS)
        self.assertGreater(
            self._wall_clock_millis(),
            allocated_ms,
            "the excursion ahead of real time must be finite and self-cancelling",
        )

        wall_clock_ms = self._wall_clock_millis()
        resynchronized_ms = self._to_epoch_millis(health.current_timestamp())
        self.assertLessEqual(
            abs(resynchronized_ms - wall_clock_ms), CLOCK_TRACKING_TOLERANCE_MS
        )

    @staticmethod
    def _wall_clock_millis():
        """Read the wall clock in whole milliseconds, as the module itself does."""
        return time.time_ns() // NANOSECONDS_PER_MILLISECOND

    @staticmethod
    def _to_epoch_millis(timestamp):
        """Turn a contract timestamp back into milliseconds since the epoch."""
        moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
        return round(moment.timestamp() * MILLISECONDS_PER_SECOND)

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
        """``render_payload`` is ``serialize(build_payload())``, fresh each call.

        The two calls are back to back with no pause: the rendered bytes must
        already differ, because the timestamp inside them is allocated fresh on
        every call rather than reused whenever two calls share a millisecond.
        """
        first = health.render_payload()
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

    def _load_isolated_module(self, serving_document=None):
        """Execute a copy of ``health.py`` from a private, controlled directory.

        With no argument the workspace stays empty, so both declared sources are
        genuinely absent and the compiled-in fallbacks are the only thing left to
        resolve from.  Given *serving_document*, that mapping is written to the
        copy's ``config/health.json`` first, which is how a test can hand the
        module a configuration it must partly honour and partly ignore.

        Either way the repository's own files are never read for configuration by
        the copy -- the module derives its paths from ``__file__`` -- and never
        written by this suite, which :meth:`tearDown` proves independently.
        """
        copy = self._workspace / pathlib.Path(health.__file__).name
        copy.write_bytes(pathlib.Path(health.__file__).resolve().read_bytes())
        if serving_document is not None:
            config_path = self._workspace / "config" / "health.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(serving_document), encoding=EXPECTED_ENCODING
            )
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
        self.assertEqual(
            module.CONFIG_PATH.exists(),
            serving_document is not None,
            "the serving source must be present exactly when one was supplied",
        )
        return module

    def test_payload_still_conforms_with_both_sources_absent(self):
        """No configuration at all, and the four-member contract still holds."""
        module = self._load_isolated_module()
        payload = module.build_payload()
        self.assert_payload_conforms(payload)
        self.assert_byte_shape(module.serialize(payload).decode(EXPECTED_ENCODING))

    def test_identity_and_path_fall_back_to_the_compiled_in_literals(self):
        """``name``, ``version`` and ``path`` come from the literals.

        The identity pair has no environment override, so with ``pyproject.toml``
        absent both values are provably the compiled-in fallbacks.  ``path`` and
        ``status`` reach the same place by a different route: their declared source
        is absent here, so resolution falls through to the literals -- and because
        the only declaration that could ever be adopted *is* the literal, they are
        those values whether the file is present, absent or hostile.  ``HOST`` and
        ``PORT`` are excluded on purpose: both accept an override, so asserting
        them by value here would make the test depend on the environment it
        happens to run in.
        """
        module = self._load_isolated_module()
        self.assertEqual(module.APP_NAME, module.FALLBACK_NAME)
        self.assertEqual(module.APP_VERSION, module.FALLBACK_VERSION)
        self.assertEqual(module.HEALTH_PATH, module.FALLBACK_PATH)
        self.assertEqual(module.APP_NAME, EXPECTED_NAME)
        self.assertEqual(module.APP_VERSION, EXPECTED_VERSION)
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)

    def test_status_needs_no_configuration_to_be_reported(self):
        """With every configuration file absent, ``status`` is still ``UP``.

        The status member is the one value whose degradation is invisible on the
        wire: with no identity source and no serving source, its chain reaches the
        compiled-in literal and the payload reports exactly what it always
        reports.  The provenance is what makes the degradation observable at all.
        """
        module = self._load_isolated_module()
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(module.build_payload()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config_sources()["status"], module.FROM_FALLBACK)
        # Nothing was declared, so nothing was rejected: an absent source is a
        # degradation, not a conflict.
        self.assertEqual(module.frozen_value_conflicts(), ())

    def test_a_configuration_file_cannot_change_the_reported_status(self):
        """A file declaring ``"DOWN"`` is honoured for serving, ignored for status.

        This is the strongest form of the assertion, and the reason it is worth
        its own test: a configuration document is handed to an isolated copy of
        the module with ``status`` set to ``DOWN`` *and* with distinctive host,
        port and path values.  The serving parameters must be adopted -- which is
        what proves the document really was read, so the test cannot pass
        vacuously -- while the payload must go on reporting ``UP``.

        A freely configurable status would let a deployment publish ``DOWN`` from a
        perfectly healthy process, and would let two tiers of one composition
        disagree about the vocabulary itself.  Every consumer of this contract --
        an orchestrator's liveness poller, any fixed-interval probe -- would then
        be reading a claim rather than a measurement.
        """
        module = self._load_isolated_module(
            serving_document={
                "host": TAMPERED_HOST,
                "port": TAMPERED_PORT,
                "path": TAMPERED_PATH,
                "status": TAMPERED_STATUS,
            }
        )
        # The document was read -- its host and port were adopted, and neither
        # matches the compiled-in fallback -- so the test cannot pass vacuously
        # against a module that ignored the file altogether.
        self.assertEqual(module.HOST, TAMPERED_HOST)
        self.assertEqual(module.PORT, TAMPERED_PORT)
        self.assertNotEqual(TAMPERED_HOST, module.FALLBACK_HOST)
        self.assertNotEqual(TAMPERED_PORT, module.FALLBACK_PORT)
        # The path in it was refused: the declared value is validated against the
        # contract before it is adopted, so the endpoint stays where every probe
        # expects it, the resolved value falls back to the literal, and the
        # rejected declaration is named in the audit rather than silently dropped.
        self.assertEqual(module.HEALTH_PATH, module.FALLBACK_PATH)
        self.assertNotEqual(module.HEALTH_PATH, TAMPERED_PATH)
        self.assertEqual(module.get_config_sources()["path"], module.FROM_FALLBACK)
        self.assertIn(
            ("path", TAMPERED_PATH, module.FALLBACK_PATH),
            module.frozen_value_conflicts(),
        )
        # The status in it was refused the same way, and reported the same way.
        payload = module.build_payload()
        self.assertEqual(payload["status"], EXPECTED_STATUS)
        self.assertNotEqual(payload["status"], TAMPERED_STATUS)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(module.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config_sources()["status"], module.FROM_FALLBACK)
        self.assertIn(
            ("status", TAMPERED_STATUS, module.STATUS_UP),
            module.frozen_value_conflicts(),
        )
        # The two legitimate members of the same document were still adopted, so
        # one refusal does not reject the whole file.
        self.assertEqual(module.get_config_sources()["host"], module.FROM_FILE)
        self.assertEqual(module.get_config_sources()["port"], module.FROM_FILE)
        # And the rest of the contract is unaffected by the tampering.
        self.assert_payload_conforms(payload)
        self.assert_byte_shape(module.serialize(payload).decode(EXPECTED_ENCODING))

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


class TestStatusIsNotConfigurable(PayloadContractAssertions, unittest.TestCase):
    """A configuration file cannot change the status this endpoint reports.

    This is a safety property, not a convenience.  The endpoint reports process
    liveness only and performs no dependency checks, so a settable status would
    let whoever edits a deployment's configuration declare a process healthy that
    is not, or unhealthy that is.  The word on the wire has to mean "this process
    answered", and it can only mean that if the declared source is validated
    rather than trusted: ``config/health.json`` is read, but the only value it
    can get adopted is the contract literal itself.

    Asserting the resolved value equals ``UP`` cannot prove this on its own: it
    passes just as happily for an implementation that reads ``status`` out of a
    file that happens to declare ``UP``.  The only assertion that distinguishes
    the two is a file declaring something *else*, so that is what this class
    builds -- and the companion assertion that a declaration restating the
    literal is adopted, with provenance ``file``, is what proves the chain is
    live rather than decorative.

    The same discipline as the fallback class applies, for the same reason: the
    mutated document is written into a private temporary directory beside a copy
    of ``health.py``, nothing in the repository is written or renamed, the copy is
    never registered in ``sys.modules``, the temporary directory is removed by a
    registered cleanup, and :meth:`tearDown` proves the two real declared sources
    are byte-for-byte unchanged.
    """

    #: A status no conforming implementation may ever report, used as the
    #: declared value precisely because reporting it would be unmistakable.
    HOSTILE_STATUS = "DOWN"

    #: A second declared value, checked to be sure the first is not being
    #: rejected merely for being a recognised word.
    ARBITRARY_STATUS = "totally-made-up"

    def setUp(self):
        """Snapshot the real declared sources and open a private temporary workspace."""
        self._identity_bytes = health.PYPROJECT_PATH.read_bytes()
        self._serving_bytes = health.CONFIG_PATH.read_bytes()
        workspace = tempfile.TemporaryDirectory(prefix="blitzy_health_status_")
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

    def _load_with_declared_status(self, declared):
        """Execute a ``health.py`` copy beside a document declaring *declared*.

        Every other setting in the temporary document is copied from the real
        one, so the only difference between this module and the imported
        ``health`` is the declared status.  The identity source is deliberately
        copied too, so ``name`` and ``version`` resolve normally and a failure
        here can only be about the status.
        """
        copy = self._workspace / pathlib.Path(health.__file__).name
        copy.write_bytes(pathlib.Path(health.__file__).resolve().read_bytes())
        (self._workspace / health.PYPROJECT_PATH.name).write_bytes(self._identity_bytes)

        document = json.loads(self._serving_bytes.decode(EXPECTED_ENCODING))
        document["status"] = declared
        serving = (
            self._workspace / health.CONFIG_PATH.parent.name / health.CONFIG_PATH.name
        )
        serving.parent.mkdir(parents=True, exist_ok=True)
        serving.write_text(json.dumps(document), encoding=EXPECTED_ENCODING)

        module, stdout_text, stderr_text = _execute_module_source(
            _HEALTH_STATUS_PROBE_MODULE_NAME, copy
        )
        self.assertEqual(stdout_text, "")
        self.assertEqual(stderr_text, "")
        # The premise of every assertion below: this copy really did resolve
        # against the temporary document, and that document really does declare
        # the hostile value.  Both are checked by reading the file back through
        # the module's own path, so the premise cannot silently stop holding.
        self.assertEqual(module.CONFIG_PATH, serving)
        self.assertTrue(module.CONFIG_PATH.is_file())
        written = json.loads(module.CONFIG_PATH.read_text(encoding=EXPECTED_ENCODING))
        self.assertEqual(module.declared_status(written), declared)
        return module

    def test_a_declared_status_is_read_but_refused_when_it_is_not_the_literal(self):
        """The document declares ``DOWN``; the endpoint still reports ``UP``.

        The refusal is asserted three ways -- the served value, the provenance and
        the audit trail -- because the served value alone would also hold for an
        implementation that never read the file at all.
        """
        module = self._load_with_declared_status(self.HOSTILE_STATUS)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(module.build_payload()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config_sources()["status"], module.FROM_FALLBACK)
        self.assertIn(
            ("status", self.HOSTILE_STATUS, module.STATUS_UP),
            module.frozen_value_conflicts(),
        )

    def test_an_arbitrary_declared_status_is_equally_powerless(self):
        """Any illegal declared value is refused, not just a recognised one."""
        module = self._load_with_declared_status(self.ARBITRARY_STATUS)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(module.build_payload()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config_sources()["status"], module.FROM_FALLBACK)
        self.assertIn(
            ("status", self.ARBITRARY_STATUS, module.STATUS_UP),
            module.frozen_value_conflicts(),
        )

    def test_a_declaration_that_restates_the_literal_is_adopted(self):
        """The other direction: a legal declaration is taken *from the file*.

        This is the assertion that proves the chain is live rather than
        decorative.  Without it, every refusal case above would pass just as
        happily for an implementation that had stopped reading the declaration
        altogether -- which is precisely a dead configuration key.
        """
        module = self._load_with_declared_status(EXPECTED_STATUS)
        self.assertEqual(module.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(module.build_payload()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config_sources()["status"], module.FROM_FILE)
        self.assertEqual(module.frozen_value_conflicts(), ())

    def test_the_serialized_body_is_unchanged_by_a_declared_status(self):
        """The bytes on the wire are unaffected, suffix and shape included."""
        module = self._load_with_declared_status(self.HOSTILE_STATUS)
        rendered = module.render_payload().decode(EXPECTED_ENCODING)
        self.assert_byte_shape(rendered)
        self.assert_payload_conforms(json.loads(rendered))
        self.assertTrue(rendered.endswith(SERIALIZED_SUFFIX))
        self.assertNotIn(self.HOSTILE_STATUS, rendered)

    def test_the_other_serving_values_are_still_read_from_the_document(self):
        """Only the refused status is affected: the rest of the chain still reads.

        Without this, a fix that stopped reading the file altogether would look
        indistinguishable from a fix that refused only the hostile status.  The
        document's ``path`` is the contract literal, so it passes validation and
        is adopted -- which is exactly what makes it a usable positive control.
        """
        module = self._load_with_declared_status(self.HOSTILE_STATUS)
        document = json.loads(self._serving_bytes.decode(EXPECTED_ENCODING))
        self.assertEqual(module.HEALTH_PATH, document["path"])
        self.assertEqual(module.APP_NAME, EXPECTED_NAME)
        self.assertEqual(module.APP_VERSION, EXPECTED_VERSION)


class TestFrozenValuesResistReconfiguration(
    PayloadContractAssertions, unittest.TestCase
):
    """No configuration document can move the endpoint or change its status.

    ``/health`` and ``UP`` are clauses of the contract, not deployment settings.
    Three tiers implement that contract independently, every probe that watches
    the endpoint targets the path literally, and the whole point of the ``status``
    member is that a caller can trust what it says -- so a file that could
    redefine either value would be a way to make a process *report* health it does
    not have, or to move the endpoint out from under every probe that watches it.
    Both values are therefore *validated* on the way in rather than trusted: the
    declaration is read, and it is adopted only when it restates the contract
    literal exactly.  This class proves it by handing the module a document that
    tries to change them.

    Two properties make the proof airtight rather than merely reassuring:

    * **A positive control in the same document.**  ``host`` and ``port`` are real
      deployment settings and are asserted to be *honoured* from the very
      document whose ``path`` and ``status`` are refused.  Without that, every
      assertion here would also pass against an implementation that ignored the
      configuration file completely.
    * **The rejection is reported, not swallowed.**  A deployment that edited the
      document expecting an effect must not get silence, so
      ``frozen_value_conflicts()`` names each rejected declaration and
      ``server.py`` prints one line per entry at start-up.

    The technique is the one :class:`TestHealthPayloadFallbackResilience` uses: a
    copy of ``health.py`` is executed beside a purpose-built document inside a
    private temporary directory.  Nothing in the repository is written, moved or
    deleted; the copy never enters ``sys.modules``; and :meth:`tearDown` proves
    both real declared sources are byte-for-byte unchanged afterwards.
    """

    def setUp(self):
        """Snapshot the real sources, open a workspace, neutralize ambient overrides."""
        self._identity_bytes = health.PYPROJECT_PATH.read_bytes()
        self._serving_bytes = health.CONFIG_PATH.read_bytes()
        workspace = tempfile.TemporaryDirectory(prefix="blitzy_health_frozen_")
        self.addCleanup(workspace.cleanup)
        self._workspace = pathlib.Path(workspace.name).resolve()
        # ``HEALTH_HOST``/``HEALTH_PORT`` outrank the document by design, so an
        # ambient override on the validation host would defeat the positive
        # control below.  Both are removed for the duration of the test and
        # restored exactly as they were -- absent stays absent -- by a cleanup
        # registered before the removal, so it runs even if setUp itself fails.
        for name in (health.ENV_HOST, health.ENV_PORT):
            self.addCleanup(self._restore_environment, name, os.environ.get(name))
            os.environ.pop(name, None)

    @staticmethod
    def _restore_environment(name, value):
        """Put one environment variable back exactly as it was, absence included."""
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

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

    def _load_with_serving_document(self, document):
        """Execute a copy of ``health.py`` beside a serving document of our choosing.

        The real ``pyproject.toml`` is copied in as well, so identity resolves
        normally and every observation belongs to the serving document under test
        rather than to a missing file.
        """
        module_source = pathlib.Path(health.__file__).resolve()
        copy = self._workspace / module_source.name
        copy.write_bytes(module_source.read_bytes())
        (self._workspace / health.PYPROJECT_PATH.name).write_bytes(
            health.PYPROJECT_PATH.read_bytes()
        )
        config_directory = self._workspace / health.CONFIG_PATH.parent.name
        config_directory.mkdir(exist_ok=True)
        (config_directory / health.CONFIG_PATH.name).write_text(
            json.dumps(document), encoding=EXPECTED_ENCODING
        )
        module, stdout_text, stderr_text = _execute_module_source(
            _HEALTH_HOSTILE_MODULE_NAME, copy
        )
        # Importing must stay silent even when the document is rejected outright:
        # reporting belongs to the entry point, never to an imported module.
        self.assertEqual(stdout_text, "")
        self.assertEqual(stderr_text, "")
        self.assertEqual(module.MODULE_DIR, self._workspace)
        self.assertTrue(
            module.CONFIG_PATH.is_file(),
            "the document under test must exist, or nothing is being tested",
        )
        self.assertTrue(module.PYPROJECT_PATH.is_file())
        return module

    def test_a_hostile_document_cannot_move_the_path_or_change_the_status(self):
        """``path``/``status`` are refused while ``host``/``port`` are honoured.

        The two acceptances are the control: they prove this document was read,
        which is what makes the two refusals meaningful.  The provenance is
        asserted alongside every value, so a refusal is visibly a refusal rather
        than a value that happens to look right.
        """
        module = self._load_with_serving_document(HOSTILE_SERVING_DOCUMENT)
        sources = module.get_config_sources()
        # Refused: the declaration was not the contract's literal.
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(module.HEALTH_STATUS, EXPECTED_STATUS)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(module.HealthRequestHandler.health_path, EXPECTED_PATH)
        self.assertNotEqual(module.HEALTH_PATH, HOSTILE_SERVING_DOCUMENT["path"])
        self.assertNotEqual(module.HEALTH_STATUS, HOSTILE_SERVING_DOCUMENT["status"])
        self.assertEqual(sources["path"], module.FROM_FALLBACK)
        self.assertEqual(sources["status"], module.FROM_FALLBACK)
        # Honoured: ordinary deployment settings, from the same document.
        self.assertEqual(module.HOST, HOSTILE_SERVING_DOCUMENT["host"])
        self.assertEqual(module.PORT, HOSTILE_SERVING_DOCUMENT["port"])
        self.assertEqual(sources["host"], module.FROM_FILE)
        self.assertEqual(sources["port"], module.FROM_FILE)

    def test_the_payload_reports_up_however_the_document_declares_the_status(self):
        """The built payload still conforms, still says ``UP``, still byte-shapes."""
        module = self._load_with_serving_document(HOSTILE_SERVING_DOCUMENT)
        payload = module.build_payload()
        self.assert_payload_conforms(payload)
        self.assertEqual(payload["status"], EXPECTED_STATUS)
        self.assert_byte_shape(module.serialize(payload).decode(EXPECTED_ENCODING))
        self.assertEqual(module.get_config()["path"], EXPECTED_PATH)
        self.assertEqual(module.get_config()["status"], EXPECTED_STATUS)
        self.assertEqual(module.get_config_sources()["status"], module.FROM_FALLBACK)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)

    def test_over_http_the_frozen_path_serves_and_the_declared_one_does_not(self):
        """The strongest form of the assertion: on the wire, against a listener.

        A document declaring ``/liveness`` produces a server that answers ``404``
        there and the full contract on ``/health``.  Nothing a configuration file
        says can create a second route or retire the only one.
        """
        module = self._load_with_serving_document(HOSTILE_SERVING_DOCUMENT)
        with _temporary_listener(module.HealthRequestHandler) as (host, port):
            served = _perform_request(host, port, "GET", EXPECTED_PATH)
            self.assertEqual(served.status, 200)
            self.assertEqual(served.header("Content-Type"), EXPECTED_CONTENT_TYPE)
            self.assertEqual(served.header("Cache-Control"), EXPECTED_CACHE_CONTROL)
            self.assert_byte_shape(served.text())
            self.assert_payload_conforms(served.json())
            self.assertEqual(served.json()["status"], EXPECTED_STATUS)

            declared = _perform_request(
                host, port, "GET", HOSTILE_SERVING_DOCUMENT["path"]
            )
            self.assertEqual(declared.status, 404)
            self.assertEqual(declared.json(), {"error": "Not Found"})

    def test_every_rejected_declaration_is_named_in_the_audit(self):
        """Both refusals are reported as ``(key, configured, frozen)`` triples.

        Silence would be the real defect: the endpoint would serve the right
        thing while the operator who edited the file believed otherwise, and the
        discrepancy would surface later as a gap in monitoring.
        """
        module = self._load_with_serving_document(HOSTILE_SERVING_DOCUMENT)
        self.assertEqual(
            module.frozen_value_conflicts(),
            (
                ("path", HOSTILE_SERVING_DOCUMENT["path"], EXPECTED_PATH),
                ("status", HOSTILE_SERVING_DOCUMENT["status"], EXPECTED_STATUS),
            ),
        )
        self.assertEqual(module.frozen_value_conflicts(), module.FROZEN_CONFLICTS)
        for key, configured, frozen in module.frozen_value_conflicts():
            with self.subTest(key=key):
                self.assertIsInstance(key, str)
                self.assertIsInstance(configured, str)
                self.assertIsInstance(frozen, str)
                self.assertNotEqual(configured, frozen)

    def test_a_document_that_restates_the_frozen_values_reports_no_conflict(self):
        """Restating ``/health`` and ``UP`` is not a conflict -- it is documentation.

        ``config/health.json`` declares both on purpose, so an operator reading
        that one file sees the whole shape of what is served.  Treating a faithful
        restatement as a conflict would make the shipped configuration warn about
        itself on every start-up.
        """
        module = self._load_with_serving_document(
            {
                "host": EXPECTED_BIND_HOST,
                "port": TIER_PORT,
                "path": EXPECTED_PATH,
                "status": EXPECTED_STATUS,
            }
        )
        self.assertEqual(module.frozen_value_conflicts(), ())
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)

    def test_omitting_the_frozen_keys_reports_no_conflict(self):
        """A document that declares only deployment settings is entirely valid."""
        module = self._load_with_serving_document({"host": "127.0.0.1", "port": 8124})
        self.assertEqual(module.frozen_value_conflicts(), ())
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(module.HOST, "127.0.0.1")
        self.assertEqual(module.PORT, 8124)

    def test_a_wrongly_typed_declaration_is_a_conflict_and_is_still_refused(self):
        """``path: 123`` is unmistakably an attempt to set the value: refused, named.

        A type error is not a reason to fall silent.  The declaration is rendered
        for the report -- so the operator sees what they actually wrote -- and the
        frozen value is served regardless.
        """
        module = self._load_with_serving_document(
            {"path": 123, "status": False, "host": "127.0.0.1", "port": 8125}
        )
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)
        self.assertEqual(
            module.frozen_value_conflicts(),
            (
                ("path", "123", EXPECTED_PATH),
                ("status", "False", EXPECTED_STATUS),
            ),
        )
        self.assert_payload_conforms(module.build_payload())

    def test_a_whitespace_padded_restatement_is_accepted_as_a_restatement(self):
        """``" /health "`` means ``/health``: surrounding whitespace is not a conflict.

        The same trimming every other configured string receives, applied here so
        that an editor's stray space cannot produce a start-up warning about a
        document that says the right thing.
        """
        module = self._load_with_serving_document(
            {"path": f"  {EXPECTED_PATH}  ", "status": f"\t{EXPECTED_STATUS}\n"}
        )
        self.assertEqual(module.frozen_value_conflicts(), ())
        self.assertEqual(module.HEALTH_PATH, EXPECTED_PATH)
        self.assertEqual(module.STATUS_UP, EXPECTED_STATUS)


class TestFrozenConflictReporting(unittest.TestCase):
    """The entry point turns a rejected declaration into an actionable warning.

    Refusing to honour a frozen value is only half of the right behaviour; the
    other half is saying so.  Silence would leave an operator who edited
    ``config/health.json`` believing something took effect, and the discrepancy
    would surface much later as a gap in monitoring instead of as a line in the
    start-up log.

    The audit itself lives in ``health.py`` (pure, silent, safe to compute at
    import) and the reporting lives in ``server.py`` (which owns every line this
    tier prints).  This class asserts the second half against the real function.
    """

    def _report(self, conflicts):
        """Run the real reporter with *conflicts* as the audit; return its output.

        Returns ``(count, lines)``.  The audit is substituted rather than
        manufactured by writing a hostile file, so the assertion is about the
        reporting behaviour alone and needs neither a temporary directory nor a
        module copy.  The substitution is undone by the context manager, so the
        real module is exactly as it was afterwards -- asserted below.
        """
        captured = io.StringIO()
        with _patched_attribute(health, "FROZEN_CONFLICTS", conflicts):
            with contextlib.redirect_stderr(captured):
                count = entry_point.report_frozen_value_conflicts()
        return count, [line for line in captured.getvalue().splitlines() if line]

    def test_one_line_names_each_rejected_declaration(self):
        """One line per conflict, each naming the key, the value and the remedy.

        Asserted through the real entry-point function rather than by re-deriving
        the wording, so this cannot pass while the reporter has been reduced to a
        no-op.
        """
        conflicts = (
            ("path", "/liveness", EXPECTED_PATH),
            ("status", "DOWN", EXPECTED_STATUS),
        )
        count, lines = self._report(conflicts)
        self.assertEqual(count, len(conflicts))
        self.assertEqual(len(lines), len(conflicts))
        for line, (key, configured, frozen) in zip(lines, conflicts, strict=True):
            with self.subTest(key=key):
                self.assertIn(key, line)
                self.assertIn(configured, line)
                self.assertIn(frozen, line)
                self.assertIn("frozen", line)
                # The remedy has to name the file to edit, or the warning is a
                # riddle: the reader learns something is wrong but not where.
                self.assertIn(health.CONFIG_PATH.name, line)
                # Attributable when several processes share one log.
                self.assertTrue(line.startswith("server.py:"), line)

    def test_a_conflict_free_configuration_produces_no_output(self):
        """No conflicts, no output: a correct deployment cannot add noise to a log."""
        count, lines = self._report(())
        self.assertEqual(count, 0)
        self.assertEqual(lines, [])

    def test_an_enormous_declaration_is_rendered_within_a_bound(self):
        """A rejected value is quoted, but never at unbounded length.

        The value is document-supplied and a document may declare one of any
        size, so rendering it whole would put that size on the error stream at
        every start-up.  It is cut to a fixed bound and marked as cut, which
        keeps the message actionable -- the leading characters are what an
        operator recognises in the file -- without letting the log inherit the
        document's length.
        """
        oversized = "D" * 4000
        count, lines = self._report((("status", oversized, EXPECTED_STATUS),))
        self.assertEqual(count, 1)
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertNotIn(oversized, line)
        self.assertIn(
            "D" * entry_point._MAX_CONFIGURED_LENGTH + entry_point._TRUNCATION_MARK,
            line,
        )
        # Bounded overall, not merely shorter than the input: the remedy prose is
        # fixed, so the whole line is a fixed cost plus the bound.
        self.assertLess(len(line), 400, line)

    def test_a_hostile_declaration_cannot_forge_a_second_log_line(self):
        """A newline in a rejected value stays inside one physical line.

        The value reaches the log from ``config/health.json``, so a value
        containing a line break would otherwise fabricate an additional entry --
        including one impersonating the start-up announcement, in a log a human
        or a parser later trusts.  Control characters are removed rather than
        escaped, so one conflict is always exactly one line.
        """
        forged = "/nope\nserver.py: listening on http://evil/health"
        count, lines = self._report((("path", forged, EXPECTED_PATH),))
        self.assertEqual(count, 1)
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("\n", lines[0])
        self.assertNotIn("\r", lines[0])
        # The readable remains of the value are still there to be recognised.
        self.assertIn("/nope", lines[0])

    def test_the_shipped_configuration_is_reported_as_conflict_free(self):
        """The real, unmodified repository configuration warns about nothing.

        The document restates ``/health`` and ``UP`` deliberately, so this is the
        assertion that the restatement is recognised as such -- and that a
        contributor running the server never sees a warning about the
        configuration the repository ships.
        """
        self.assertEqual(health.frozen_value_conflicts(), ())
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            count = entry_point.report_frozen_value_conflicts()
        self.assertEqual(count, 0)
        self.assertEqual(captured.getvalue(), "")

    def test_the_startup_line_announces_the_frozen_path(self):
        """The one line on standard output points at ``/health``, never elsewhere.

        A start-up banner that named a configurable path would be the one place a
        rejected declaration could still mislead a reader, so it is pinned here
        together with the identity the payload reports.
        """
        line = entry_point.startup_line(LOOPBACK_HOST, 8126)
        self.assertIn(EXPECTED_PATH, line)
        self.assertIn(f"{LOOPBACK_HOST}:8126", line)
        self.assertIn(EXPECTED_NAME, line)
        self.assertIn(EXPECTED_VERSION, line)

    def test_importing_the_entry_point_binds_nothing_and_prints_nothing(self):
        """A self-guard on this file's own import: ``server.py`` is inert to import.

        The whole reason this suite can import the entry point is that importing
        it defines functions and returns.  If that ever stopped being true, every
        run of this file would bind port 8000 and the run would fail for a reason
        with no connection to the code under test -- so it is asserted rather than
        trusted.
        """
        module_path = pathlib.Path(entry_point.__file__).resolve()
        probe, stdout_text, stderr_text = _execute_module_source(
            "blitzy_probe_server_import", module_path
        )
        self.assertEqual(stdout_text, "")
        self.assertEqual(stderr_text, "")
        self.assertEqual(probe.DEFAULT_PORT, TIER_PORT)
        self.assertEqual(probe.DEFAULT_HOST, EXPECTED_BIND_HOST)
        self.assertIn("report_frozen_value_conflicts", probe.__all__)
class TestRequestTargetPath(unittest.TestCase):
    """The normative path rule, asserted directly and with no socket in play.

    ``health.py`` exposes the rule as a plain function precisely so it can be
    asserted this way -- fast, exhaustive and independent of any listener -- in
    the same shape the JavaScript and Java tiers expose theirs.  The wire-level
    consequences are asserted separately, against a real listener, in
    :class:`TestHealthEndpointOverHttp`.

    The rule exists because delegating the decision to a URI parser gives one
    documented contract three different sets of undocumented aliases: CPython's
    :func:`urllib.parse.urlsplit` reads ``http:/health`` as the path
    ``/health``, the WHATWG parser resolves ``/./health`` to ``/health``, and
    ``java.net.URI.getPath`` percent-decodes ``/%68ealth`` to ``/health``.  Every
    one of those spellings appears in :data:`TARGET_PATH_CASES` with the answer
    the contract requires instead.
    """

    def test_every_target_resolves_exactly_as_the_rule_specifies(self):
        """Each transmitted target maps to exactly one path, or to nothing."""
        for target, expected in TARGET_PATH_CASES:
            with self.subTest(target=target):
                self.assertEqual(health.request_target_path(target), expected)

    def test_no_alias_spelling_resolves_to_the_served_path(self):
        """Not one entry in the unserved table can be mistaken for the endpoint.

        Stated as its own claim rather than left implicit in the table above: the
        contract admits exactly one spelling, and this is the assertion that says
        so about the whole set at once.
        """
        for target in UNSERVED_TARGETS:
            with self.subTest(target=target):
                self.assertNotEqual(
                    health.request_target_path(target),
                    EXPECTED_PATH,
                    f"{target!r} must not be an alias for {EXPECTED_PATH}",
                )

    def test_every_served_spelling_resolves_to_the_served_path(self):
        """The tolerated spellings -- query, empty query, fragment -- all match."""
        for target in SERVED_TARGETS:
            with self.subTest(target=target):
                self.assertEqual(health.request_target_path(target), EXPECTED_PATH)

    def test_an_unusable_target_matches_nothing_rather_than_raising(self):
        """A missing or non-string target is unmatchable, never an exception.

        The handler calls this with whatever it could recover from the request
        line, so the rule has to fail closed: an unusable target produces a
        ``404``, not a traceback and not an accidental match.
        """
        for target in (None, "", 0, b"/health", ["/health"]):
            with self.subTest(target=target):
                self.assertIsNone(health.request_target_path(target))
class TestCoercionPrimitives(unittest.TestCase):
    """The two coercion helpers on which the whole precedence chain rests.

    ``_resolve_host`` and ``_resolve_port`` are thin: they choose *which* candidate
    to offer, while these two functions decide whether a candidate is usable at
    all.  Every "falls through to the next link" behaviour in this tier is
    therefore ultimately a property of these functions, and they are pure, so the
    full matrix is cheap to state exactly once here.
    """

    def test_only_a_non_blank_string_is_accepted_as_text(self):
        """A usable string is stripped and returned; everything else falls through."""
        sentinel = object()
        for candidate, expected in (
            ("127.0.0.5", "127.0.0.5"),
            ("  127.0.0.5  ", "127.0.0.5"),
            ("\t0.0.0.0\n", "0.0.0.0"),
            ("", sentinel),
            ("   ", sentinel),
            ("\t\n", sentinel),
            (None, sentinel),
            (8000, sentinel),
            (True, sentinel),
            ([], sentinel),
            ({}, sentinel),
            (b"127.0.0.5", sentinel),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(health._coerce_text(candidate, sentinel), expected)

    def test_only_an_in_range_port_is_accepted(self):
        """Integers and decimal strings inside the bounds; everything else falls through."""
        sentinel = object()
        for candidate, expected in (
            (8000, 8000),
            (1, 1),
            (65535, 65535),
            ("8000", 8000),
            ("  8199  ", 8199),
            ("1", 1),
            ("65535", 65535),
            # ``0`` asks the kernel to choose a port, which no configured value may
            # do: see :data:`health._MIN_PORT`.
            (0, sentinel),
            ("0", sentinel),
            (-1, sentinel),
            (65536, sentinel),
            (70000, sentinel),
            ("-1", sentinel),
            ("65536", sentinel),
            ("abc", sentinel),
            ("80.5", sentinel),
            # ``int(value, 10)`` accepts PEP 515 digit separators, so this is a
            # usable port.  Asserted rather than left implicit: it is inherited
            # behaviour, it is harmless, and a reader should not have to guess.
            ("8_000", 8000),
            ("0x1f90", sentinel),
            ("", sentinel),
            ("   ", sentinel),
            (None, sentinel),
            (8000.0, sentinel),
            ([8000], sentinel),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(health._coerce_port(candidate, sentinel), expected)

    def test_a_boolean_is_never_read_as_a_port(self):
        """``True`` is an ``int`` subclass, but ``HEALTH_PORT=True`` is a mistake.

        Without an explicit ``bool`` rejection, ``True`` would silently resolve to
        port 1 -- a privileged port, and never what the operator meant.
        """
        sentinel = object()
        self.assertIs(health._coerce_port(True, sentinel), sentinel)
        self.assertIs(health._coerce_port(False, sentinel), sentinel)

    def test_bounds_are_inclusive_and_exclude_the_ephemeral_sentinel(self):
        """Both bounds resolve; ``0`` is below the floor at every tier.

        ``0`` asks the operating system to choose a port at random, so a
        *configured* ``0`` would bind an address no probe configured in advance
        could reach -- the endpoint running and unreachable at once.  All three
        tiers reject it identically.  A test that wants an ephemeral port passes
        ``0`` to the server constructor instead, which is a bind-time argument.
        """
        self.assertEqual(health._MIN_PORT, 1)
        self.assertEqual(health._MAX_PORT, 65535)
        self.assertEqual(health._coerce_port(health._MIN_PORT, None), 1)
        self.assertEqual(health._coerce_port(health._MAX_PORT, None), 65535)
        self.assertIsNone(health._coerce_port(0, None), "a configured 0 must not resolve")
        self.assertIsNone(health._coerce_port("0", None))


class TestBindOverridePrecedence(unittest.TestCase):
    """``HEALTH_HOST``/``HEALTH_PORT`` -> ``config/health.json`` -> literal, behaviourally.

    Asserting that ``health.ENV_HOST == "HEALTH_HOST"`` proves only that a name was
    spelled correctly.  It would still pass if the override were read and then
    discarded, if a blank value short-circuited the chain, or if an unusable
    override skipped the configuration file and jumped straight to the literal.
    This class therefore resolves the chain for real and asserts the value that
    comes out.

    Two properties make that safe to do.  The chain is applied once at import time,
    so each case executes a *copy* of ``health.py`` inside a private temporary
    directory holding purpose-written configuration files -- the repository's own
    files are never read for these assertions and never written at all.  And every
    case sets both variables explicitly, including to "absent", so no case can be
    influenced by the environment the suite happens to run in, nor leak into the
    next: :func:`_environment` restores exactly what was there before.

    The configured values are deliberately *not* the tier defaults.  A test whose
    expected value equals the fallback cannot tell the two links apart, so the
    workspace configures ``127.0.0.9:8123`` and the overrides use ``127.0.0.4:8199``
    -- three distinguishable answers for three distinguishable links.
    """

    def setUp(self):
        """Snapshot the real sources and open a private temporary workspace."""
        self._identity_bytes = health.PYPROJECT_PATH.read_bytes()
        self._serving_bytes = health.CONFIG_PATH.read_bytes()
        self._environment_before = dict(os.environ)
        workspace = tempfile.TemporaryDirectory(prefix="blitzy_health_precedence_")
        self.addCleanup(workspace.cleanup)
        self._workspace = pathlib.Path(workspace.name).resolve()

    def tearDown(self):
        """Prove the repository's sources and the process environment are unchanged."""
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
        self.assertEqual(
            dict(os.environ),
            self._environment_before,
            "every override must be restored, or later tests are not trustworthy",
        )

    def _write_serving_document(self, document):
        """Write *document* as the workspace's ``config/health.json``."""
        config_dir = self._workspace / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "health.json").write_text(
            json.dumps(document), encoding=EXPECTED_ENCODING
        )

    def _resolve(self, host_override, port_override, serving_document=None):
        """Resolve the chain in an isolated copy and return its ``get_config()``."""
        return self._resolve_with_sources(
            host_override, port_override, serving_document
        )[0]

    def _resolve_with_sources(
        self, host_override, port_override, serving_document=None
    ):
        """Resolve the chain in an isolated copy; return ``(config, sources)``.

        *host_override* and *port_override* are applied to the environment for the
        duration of the module's execution only; ``None`` means the variable is
        genuinely absent, which is a different input from an empty string.
        *serving_document* is written as the workspace's configuration file, or the
        file is left absent when it is ``None``.
        """
        if serving_document is not None:
            self._write_serving_document(serving_document)
        copy = self._workspace / pathlib.Path(health.__file__).name
        copy.write_bytes(pathlib.Path(health.__file__).resolve().read_bytes())
        with _environment(
            **{health.ENV_HOST: host_override, health.ENV_PORT: port_override}
        ):
            module, stdout_text, stderr_text = _execute_module_source(
                _HEALTH_PROBE_MODULE_NAME, copy
            )
        self.assertEqual(stdout_text, "", "importing health.py must stay silent")
        self.assertEqual(stderr_text, "", "importing health.py must stay silent")
        self.assertEqual(module.MODULE_DIR, self._workspace)
        return module.get_config(), module.get_config_sources()

    #: The workspace's configured serving document: distinguishable from both the
    #: overrides and the compiled-in literals.
    _CONFIGURED = {
        "host": ISOLATED_CONFIG_HOST,
        "port": ISOLATED_CONFIG_PORT,
        "path": EXPECTED_PATH,
        "status": EXPECTED_STATUS,
    }

    def test_with_no_override_the_configuration_file_supplies_both_values(self):
        """Link two of the chain: the file is read, and its values are used."""
        config = self._resolve(None, None, self._CONFIGURED)
        self.assertEqual(config["host"], ISOLATED_CONFIG_HOST)
        self.assertEqual(config["port"], ISOLATED_CONFIG_PORT)
        self.assertNotEqual(
            config["host"], health.FALLBACK_HOST, "the file must beat the literal"
        )
        self.assertNotEqual(config["port"], health.FALLBACK_PORT)

    def test_a_usable_override_beats_the_configuration_file(self):
        """Link one of the chain: the environment wins over a perfectly good file."""
        config = self._resolve(OVERRIDE_HOST, str(OVERRIDE_PORT), self._CONFIGURED)
        self.assertEqual(config["host"], OVERRIDE_HOST)
        self.assertEqual(config["port"], OVERRIDE_PORT)

    def test_an_override_is_stripped_before_it_is_used(self):
        """Surrounding whitespace in an override is not part of the value.

        A variable set from a shell script or a compose file routinely carries a
        stray space, and an unstripped host would fail to bind for a reason the
        operator cannot see.
        """
        config = self._resolve(
            f"  {OVERRIDE_HOST}  ", f"\t{OVERRIDE_PORT}\n", self._CONFIGURED
        )
        self.assertEqual(config["host"], OVERRIDE_HOST)
        self.assertEqual(config["port"], OVERRIDE_PORT)

    def test_a_blank_override_falls_through_to_the_configuration_file(self):
        """``HEALTH_HOST=`` is a set-but-unusable value, not an instruction.

        This is the case a name-only assertion cannot distinguish: a blank value
        must neither be bound as an empty host nor short-circuit the chain.
        """
        for blank in ("", "   ", "\t\n"):
            with self.subTest(blank=blank):
                config = self._resolve(blank, blank, self._CONFIGURED)
                self.assertEqual(config["host"], ISOLATED_CONFIG_HOST)
                self.assertEqual(config["port"], ISOLATED_CONFIG_PORT)

    def test_an_unusable_port_override_degrades_one_link_at_a_time(self):
        """A bad ``HEALTH_PORT`` falls through to the file, not straight to the literal.

        The distinction is the whole point of the chain: an operator who typos the
        variable should still get the port the repository configured, not the port
        the code was compiled with.
        """
        for unusable in ("abc", "70000", "-1", "65536", "80.5", "True", "0x1f90"):
            with self.subTest(unusable=unusable):
                config = self._resolve(None, unusable, self._CONFIGURED)
                self.assertEqual(
                    config["port"],
                    ISOLATED_CONFIG_PORT,
                    "an unusable override must fall through to the file",
                )
                self.assertNotEqual(config["port"], health.FALLBACK_PORT)

    def test_the_ephemeral_port_is_not_a_usable_configured_value(self):
        """``HEALTH_PORT=0`` is rejected and falls through to the next link.

        ``0`` asks the operating system to choose a port, and a health endpoint on
        a port chosen at bind time cannot be reached by anything configured in
        advance -- the process would be running and unreachable at once.  All three
        tiers reject it identically.  A test that wants an ephemeral listener passes
        ``0`` to ``server.create_server``, which is a bind-time argument and not a
        configured value.
        """
        config = self._resolve(None, "0", self._CONFIGURED)
        self.assertEqual(config["port"], ISOLATED_CONFIG_PORT)
        self.assertNotEqual(config["port"], 0)

    def test_each_variable_is_resolved_independently(self):
        """Overriding the host does not disturb the port, and vice versa."""
        host_only = self._resolve(OVERRIDE_HOST, None, self._CONFIGURED)
        self.assertEqual(host_only["host"], OVERRIDE_HOST)
        self.assertEqual(host_only["port"], ISOLATED_CONFIG_PORT)

        port_only = self._resolve(None, str(OVERRIDE_PORT), self._CONFIGURED)
        self.assertEqual(port_only["host"], ISOLATED_CONFIG_HOST)
        self.assertEqual(port_only["port"], OVERRIDE_PORT)

    def test_an_override_wins_even_when_the_configuration_file_is_absent(self):
        """With no file at all, a usable override still supplies the value."""
        config = self._resolve(OVERRIDE_HOST, str(OVERRIDE_PORT), None)
        self.assertFalse((self._workspace / "config" / "health.json").exists())
        self.assertEqual(config["host"], OVERRIDE_HOST)
        self.assertEqual(config["port"], OVERRIDE_PORT)

    def test_an_override_wins_even_when_the_configuration_file_is_unusable(self):
        """A malformed file does not stop a good override from being honoured."""
        config_dir = self._workspace / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "health.json").write_text("{not json", encoding=EXPECTED_ENCODING)
        config = self._resolve(OVERRIDE_HOST, str(OVERRIDE_PORT))
        self.assertEqual(config["host"], OVERRIDE_HOST)
        self.assertEqual(config["port"], OVERRIDE_PORT)

    def test_with_no_usable_link_the_literals_answer(self):
        """Link three: an unusable override and no file, and the endpoint still serves.

        Both values resolve to the compiled-in literals, which is the property that
        keeps a deployment that did not ship the configuration file able to
        report health.
        """
        config = self._resolve("   ", "not-a-port", None)
        self.assertEqual(config["host"], health.FALLBACK_HOST)
        self.assertEqual(config["port"], health.FALLBACK_PORT)
        self.assertEqual(config["host"], EXPECTED_BIND_HOST)
        self.assertEqual(config["port"], TIER_PORT)

    def test_a_wrongly_typed_configured_value_falls_through_to_the_literal(self):
        """A file whose ``host``/``port`` are the wrong type resolves to the literals."""
        for document in (
            {"host": 8000, "port": "not-a-port"},
            {"host": None, "port": None},
            {"host": [], "port": {}},
            {"host": "", "port": True},
            {"host": "   ", "port": 70000},
            {},
        ):
            with self.subTest(document=document):
                config = self._resolve(None, None, document)
                self.assertEqual(config["host"], health.FALLBACK_HOST)
                self.assertEqual(config["port"], health.FALLBACK_PORT)

    def test_identity_has_no_environment_override(self):
        """``name`` and ``version`` are declared by the repository, not the deployment.

        Setting the two documented variables must not be able to rename the
        application: identity comes from ``pyproject.toml`` alone.  A tier that let
        the environment rewrite its own name would make the ``name`` field
        worthless for telling one tier's response from another's.
        """
        (self._workspace / "pyproject.toml").write_text(
            '[project]\nname = "isolated_identity"\nversion = "9.9.9"\n',
            encoding=EXPECTED_ENCODING,
        )
        with _environment(
            **{
                "HEALTH_NAME": "spoofed",
                "HEALTH_VERSION": "0.0.0",
                "APP_NAME": "spoofed",
                "APP_VERSION": "0.0.0",
            }
        ):
            config = self._resolve(None, None, self._CONFIGURED)
        self.assertEqual(config["name"], "isolated_identity")
        self.assertEqual(config["version"], "9.9.9")

    def test_the_path_and_status_have_no_environment_override(self):
        """The resource path and the status literal have no environment layer.

        Both are resolved settings, but their chain is ``config/health.json`` then
        the compiled-in literal -- there is no variable to set, so inventing
        plausible names for one changes nothing.  The workspace's document declares
        both legally, so both resolve *from the file*, which is what proves the
        chain is live rather than that the environment was merely ignored.
        """
        with _environment(
            **{"HEALTH_PATH": "/spoofed", "HEALTH_STATUS": "DOWN", "PATH_OVERRIDE": "/x"}
        ):
            config, sources = self._resolve_with_sources(None, None, self._CONFIGURED)
        self.assertEqual(config["path"], EXPECTED_PATH)
        self.assertEqual(config["status"], EXPECTED_STATUS)
        self.assertEqual(sources["path"], health.FROM_FILE)
        self.assertEqual(sources["status"], health.FROM_FILE)
        self.assertEqual(health.STATUS_UP, EXPECTED_STATUS)

    def test_a_relative_configured_path_is_rejected_for_the_literal(self):
        """A path that cannot match a parsed request target is not used."""
        for candidate in ("health", "./health", "", "   ", 8000, None):
            with self.subTest(candidate=candidate):
                config = self._resolve(
                    None, None, {"host": ISOLATED_CONFIG_HOST, "path": candidate}
                )
                self.assertEqual(config["path"], health.FALLBACK_PATH)

    def test_the_real_module_still_resolves_the_repository_values(self):
        """A self-guard: none of the above disturbed the imported ``health`` module."""
        config = health.get_config()
        self.assertEqual(config["name"], EXPECTED_NAME)
        self.assertEqual(config["version"], EXPECTED_VERSION)
        self.assertEqual(config["path"], EXPECTED_PATH)
        self.assertEqual(config["status"], EXPECTED_STATUS)
        self.assertEqual(health.STATUS_UP, EXPECTED_STATUS)
        # The repository ships a document that declares every value legally, so
        # nothing may report the fallback and nothing may be in conflict.
        self.assertEqual(
            health.get_config_sources(),
            {
                "name": health.FROM_FILE,
                "version": health.FROM_FILE,
                "host": health.FROM_FILE,
                "port": health.FROM_FILE,
                "path": health.FROM_FILE,
                "status": health.FROM_FILE,
            },
        )
        self.assertEqual(health.frozen_value_conflicts(), ())


class TestResolverPrecedenceDirectly(unittest.TestCase):
    """The two resolvers, called directly against a controlled environment.

    :class:`TestBindOverridePrecedence` proves the chain end-to-end through a real
    module execution, which is the assertion that matters.  These cases call
    ``_resolve_host`` and ``_resolve_port`` straight, which pins *where* in the
    chain each decision is made and runs the matrix without a module load per case.
    """

    def setUp(self):
        """Record the environment so the restoration can be asserted."""
        self._environment_before = dict(os.environ)

    def tearDown(self):
        """Prove every override was restored exactly."""
        self.assertEqual(dict(os.environ), self._environment_before)

    def test_host_precedence_matrix(self):
        """Environment, then document, then literal -- value *and* provenance.

        Each row asserts the pair the resolver returns, so the link that supplied
        the value is pinned as well as the value itself.  Without the label, a row
        whose expected value happens to equal the next link's value would pass for
        a resolver that skipped a link entirely.
        """
        document = {"host": ISOLATED_CONFIG_HOST}
        for override, expected in (
            (OVERRIDE_HOST, (OVERRIDE_HOST, health.FROM_ENVIRONMENT)),
            (f"  {OVERRIDE_HOST} ", (OVERRIDE_HOST, health.FROM_ENVIRONMENT)),
            ("", (ISOLATED_CONFIG_HOST, health.FROM_FILE)),
            ("   ", (ISOLATED_CONFIG_HOST, health.FROM_FILE)),
            (None, (ISOLATED_CONFIG_HOST, health.FROM_FILE)),
        ):
            with self.subTest(override=override):
                with _environment(**{health.ENV_HOST: override}):
                    self.assertEqual(health._resolve_host(document), expected)
        for override, expected in (
            (OVERRIDE_HOST, (OVERRIDE_HOST, health.FROM_ENVIRONMENT)),
            ("", (health.FALLBACK_HOST, health.FROM_FALLBACK)),
            (None, (health.FALLBACK_HOST, health.FROM_FALLBACK)),
        ):
            with self.subTest(override=override, document="empty"):
                with _environment(**{health.ENV_HOST: override}):
                    self.assertEqual(health._resolve_host({}), expected)

    def test_port_precedence_matrix(self):
        """An unusable override falls to the document, then the document to the literal.

        ``0`` is among the unusable values, at both links: a configured port the
        kernel chooses cannot be reached by a probe configured in advance, so it is
        rejected identically at all three tiers.
        """
        document = {"port": ISOLATED_CONFIG_PORT}
        environment_wins = (OVERRIDE_PORT, health.FROM_ENVIRONMENT)
        file_wins = (ISOLATED_CONFIG_PORT, health.FROM_FILE)
        for override, expected in (
            (str(OVERRIDE_PORT), environment_wins),
            (f" {OVERRIDE_PORT} ", environment_wins),
            ("0", file_wins),
            ("abc", file_wins),
            ("70000", file_wins),
            ("-1", file_wins),
            ("True", file_wins),
            ("", file_wins),
            (None, file_wins),
        ):
            with self.subTest(override=override):
                with _environment(**{health.ENV_PORT: override}):
                    self.assertEqual(health._resolve_port(document), expected)
        literal_wins = (health.FALLBACK_PORT, health.FROM_FALLBACK)
        for override, expected in (
            (str(OVERRIDE_PORT), environment_wins),
            ("abc", literal_wins),
            (None, literal_wins),
        ):
            with self.subTest(override=override, document="unusable"):
                with _environment(**{health.ENV_PORT: override}):
                    self.assertEqual(health._resolve_port({"port": "nope"}), expected)
        # A configured 0 is as unusable as a configured "nope": both fall through.
        with _environment(**{health.ENV_PORT: None}):
            self.assertEqual(health._resolve_port({"port": 0}), literal_wins)

    def test_an_absent_variable_is_not_the_same_input_as_a_blank_one(self):
        """Both resolve to the next link, but by different routes -- so both are tested."""
        host_literal = (health.FALLBACK_HOST, health.FROM_FALLBACK)
        port_literal = (health.FALLBACK_PORT, health.FROM_FALLBACK)
        with _environment(**{health.ENV_HOST: None, health.ENV_PORT: None}):
            self.assertNotIn(health.ENV_HOST, os.environ)
            self.assertNotIn(health.ENV_PORT, os.environ)
            self.assertEqual(health._resolve_host({}), host_literal)
            self.assertEqual(health._resolve_port({}), port_literal)
        with _environment(**{health.ENV_HOST: "", health.ENV_PORT: ""}):
            self.assertEqual(os.environ[health.ENV_HOST], "")
            self.assertEqual(os.environ[health.ENV_PORT], "")
            self.assertEqual(health._resolve_host({}), host_literal)
            self.assertEqual(health._resolve_port({}), port_literal)

    def test_the_environment_helper_restores_a_pre_existing_value(self):
        """A self-guard on the fixture: nesting restores the outer value, not the default."""
        with _environment(**{health.ENV_HOST: "outer"}):
            self.assertEqual(os.environ[health.ENV_HOST], "outer")
            with _environment(**{health.ENV_HOST: "inner"}):
                self.assertEqual(os.environ[health.ENV_HOST], "inner")
            self.assertEqual(os.environ[health.ENV_HOST], "outer")
            with _environment(**{health.ENV_HOST: None}):
                self.assertNotIn(health.ENV_HOST, os.environ)
            self.assertEqual(os.environ[health.ENV_HOST], "outer")

    def test_the_environment_helper_restores_after_an_exception(self):
        """Restoration happens on the failure path too, or one failure poisons the run."""
        marker = RuntimeError("deliberate")
        with self.assertRaises(RuntimeError) as captured:
            with _environment(**{health.ENV_PORT: "9999"}):
                self.assertEqual(os.environ[health.ENV_PORT], "9999")
                raise marker
        self.assertIs(captured.exception, marker)
        self.assertEqual(dict(os.environ), self._environment_before)



class TestHealthEndpointOverHttp(PayloadContractAssertions, unittest.TestCase):
    """The contract asserted over real HTTP, against a real listener.

    Everything about how this listener is run is a safety requirement:

    * **``("127.0.0.1", 0)``.**  Port 0 has the operating system assign a free
      port, read back from ``server_address``; the tier's declared port 8000 is
      never bound, so this suite passes whether or not the real server is running.
      ``127.0.0.1`` keeps the socket off every other interface -- a test must not
      publish a service.
    * **A daemon thread**, so the listener cannot outlive the interpreter even if
      teardown were somehow skipped.
    * **Guaranteed teardown.**  ``shutdown()`` then ``server_close()``, in a
      ``finally``, with the thread joined and its death asserted.  A leaked
      listener or a hung suite would stall whatever ran it.
    * **Polled readiness and bounded requests**, so a defect surfaces as a
      failure instead of a hang.

    The listener here is assembled from the handler directly rather than by
    starting ``server.py``, which is the whole reason ``health.py`` never binds a
    socket of its own: a handler-level fixture is fast, needs no process and
    cannot be affected by the entry point's own configuration.  That is a
    deliberate division of labour, not a gap -- ``server.py`` is exercised as a
    real process by :class:`TestServerProcessLifecycle`, which gives it a
    reserved free port so it too never binds 8000.
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
    def _perform(cls, method, target, body=None, headers=None):
        """Perform one bounded request against this class's listener.

        Delegates to :func:`_perform_request` so that every well-behaved request in
        this file -- against this fixture or against a ``server.py`` child process --
        shares one timeout, one close guarantee and one snapshot type.
        """
        return _perform_request(cls._host, cls._port, method, target, body, headers)

    @classmethod
    def _raw(cls, timeout=REQUEST_TIMEOUT_SECONDS):
        """Open a raw connection to this class's listener, registered for cleanup.

        Used only by the framing and parser-safety tests, which send requests
        ``http.client`` refuses to construct.
        """
        return _RawConnection(cls._host, cls._port, timeout)

    def _assert_contract_headers(self, response):
        """Assert the headers every response carries, and an accurate length."""
        self.assertEqual(response.header("Content-Type"), EXPECTED_CONTENT_TYPE)
        self.assertEqual(response.header("Cache-Control"), EXPECTED_CACHE_CONTROL)
        self.assertEqual(response.header("Content-Length"), str(len(response.body)))

    # The listener itself.

    def test_listener_is_loopback_only_and_never_the_tier_port(self):
        """A self-guard: this suite binds an ephemeral loopback port, not 8000.

        Asserted as a test rather than left to review, because binding the tier's
        real port is the one mistake in this file that would fail the suite for a
        reason unrelated to the code under test.
        """
        self.assertEqual(self._host, LOOPBACK_HOST)
        self.assertNotEqual(self._port, TIER_PORT)
        self.assertNotEqual(self._port, EPHEMERAL_PORT)
        self.assertGreater(self._port, 0)
        self.assertLessEqual(self._port, 65535)
        self.assertEqual(self._server.server_address[0], LOOPBACK_HOST)

    # Success paths.

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

        Recorded as an assertion because the sibling Java tier's runtime emits
        ``Content-type`` and ``Cache-control``.  Field names are case-insensitive
        and the values are exact, so any probe of this contract must match names
        case-insensitively rather than by exact string.
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

    # Negative paths.

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
        method is not permitted anywhere on this server.  ``TRACE``, ``PROPFIND``
        and ``MKCOL`` are in the table because an implementation that enumerated
        ``do_*`` methods would answer those ``501``, which is not the contract.
        """
        for method in REFUSED_METHODS:
            with self.subTest(method=method):
                response = self._perform(method, EXPECTED_PATH)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)
                self.assertEqual(response.json(), {"error": "Method Not Allowed"})
                self._assert_contract_headers(response)

    def test_an_unrecognised_method_token_is_refused_as_a_method(self):
        """An undefined token answers 405 too, and never claims to be healthy.

        Recorded as an assertion because the three tiers genuinely differ here,
        and a probe must not assume otherwise: this tier and the Java tier route
        an unknown token to the ``405`` responder, while the JavaScript tier's
        HTTP parser validates the token against a fixed table and answers its own
        ``400`` before any handler runs.  What all three guarantee -- and what
        this test pins -- is that an unknown method never yields ``200`` and never
        yields a ``5xx``.  Lowercase ``get`` is included because method names are
        case-sensitive: it is a different token from ``GET``, not a spelling of
        it.
        """
        for method in UNRECOGNISED_METHOD_TOKENS:
            with self.subTest(method=method):
                response = self._perform(method, EXPECTED_PATH)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)
                self.assertEqual(response.json(), {"error": "Method Not Allowed"})
                self.assertNotIn(b"status", response.body)

    # -- Exactly one spelling of the target is served ------------------------

    def test_every_tolerated_spelling_is_served(self):
        """A query string, an empty query and a fragment are all the same resource."""
        for target in SERVED_TARGETS:
            with self.subTest(target=target):
                response = self._perform("GET", target)
                self.assertEqual(response.status, 200)
                self._assert_contract_headers(response)
                self.assert_byte_shape(response.text())
                self.assert_payload_conforms(response.json())

    def test_the_absolute_form_target_a_proxy_sends_is_not_served(self):
        """``GET http://host:port/health`` is a different target, and answers 404.

        Absolute-form carries its own authority, so honouring it would make this
        server answer for any host name a caller chose to write -- a second
        spelling of the one resource this endpoint serves.  The request is still
        *accepted* and still receives a well-formed, contract-defined response;
        that response is the contract's ``404``.  Asserted against this
        listener's real authority, not a fabricated one, so the refusal cannot be
        mistaken for a mismatched-host rejection.
        """
        origin = f"http://{self._host}:{self._port}"
        for target in (
            f"{origin}{EXPECTED_PATH}",
            f"{origin}{EXPECTED_PATH}?x=1",
            f"{origin}/%68ealth",
        ):
            with self.subTest(target=target):
                response = self._perform("GET", target)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.json(), {"error": "Not Found"})
                self._assert_contract_headers(response)

    def test_no_alias_spelling_is_served(self):
        """Every spelling in :data:`UNSERVED_TARGETS` answers the handler's own 404.

        This is the wire-level form of the "exactly one spelling" clause, and it
        is asserted over a raw target list rather than through a URL builder
        precisely because a builder would normalize the very spellings under
        test.  Each response must be the handler's compact JSON ``404`` -- not a
        ``200``, not a runtime-generated HTML page, and never a body containing a
        ``status`` member, which would mean the payload had leaked onto an
        unserved route.
        """
        for target in UNSERVED_TARGETS:
            with self.subTest(target=target):
                response = self._perform("GET", target)
                self.assertEqual(
                    response.status,
                    404,
                    f"{target!r} must not be an alias for {EXPECTED_PATH}",
                )
                self._assert_contract_headers(response)
                self.assertEqual(response.json(), {"error": "Not Found"})
                self.assertNotIn(b"status", response.body)

    def test_head_on_an_alias_is_a_404_with_no_body(self):
        """``HEAD`` follows exactly the same routing as ``GET``.

        A ``HEAD`` that answered ``200`` where a ``GET`` answered ``404`` would
        let a probe using the cheaper method reach a resource the contract does
        not publish.  ``Content-Length`` still reports the length the ``GET``
        body would have had, so the framing stays honest.
        """
        for target in UNSERVED_TARGETS:
            with self.subTest(target=target):
                response = self._perform("HEAD", target)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.body, b"")
                self.assertEqual(
                    response.header("Content-Length"),
                    str(len(health.NOT_FOUND_BODY)),
                )
                self.assertEqual(response.header("Content-Type"), EXPECTED_CONTENT_TYPE)

    def test_method_is_checked_before_path_for_an_alias_too(self):
        """``POST`` on an alias is a 405, not a 404: the method is rejected first."""
        for target in ("/%68ealth", "///health", "*"):
            with self.subTest(target=target):
                response = self._perform("POST", target)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)

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

    def test_a_doubled_leading_slash_is_not_an_alias_for_the_endpoint(self):
        """``GET //health`` answers 404, despite the standard library's rewrite.

        CPython's ``parse_request`` collapses a run of leading slashes in the
        request target, so a client asking for ``//health`` is presented to
        application code as ``/health``.  That rewrite exists to stop a handler
        which *redirects* from emitting a protocol-relative ``Location``; this
        handler has no 3xx branch at all, so the rewrite would protect nothing here
        while silently creating undocumented aliases for the one resource this
        module serves.  The handler therefore inspects the raw request line and
        answers 404 -- and if that guard were removed, every other route test in
        this class would still pass, which is exactly why this one exists.
        """
        response = self._perform("GET", f"/{EXPECTED_PATH}")
        self.assertEqual(response.status, 404, "//health must not reach the payload")
        self._assert_contract_headers(response)
        self.assertEqual(response.json(), {"error": "Not Found"})
        self.assert_serialization_is_compact(response.text())
    def test_case_is_not_normalized(self):
        """``GET /HEALTH`` answers 404: paths are case-sensitive, as HTTP says."""
        response = self._perform("GET", EXPECTED_PATH.upper())
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json(), {"error": "Not Found"})

    # -- No alias reaches the endpoint -------------------------------------

    def test_no_normalizing_alias_reaches_the_endpoint(self):
        """Every target in :data:`ALIASED_REQUEST_TARGETS` answers 404.

        Each of them resolves to ``/health`` under some parser's normalization --
        dot-segment collapsing, percent-decoding, or resolution against a base URL
        that discards the authority.  The handler compares the *raw* origin-form
        path against the resolved served path and normalizes nothing, so each one
        is a different path and each one is a 404.

        Why this matters beyond tidiness: an alias is an undocumented route.  A
        probe watching ``/health`` and an operator reading the contract would agree
        on one resource while the server answered on many, and a request naming a
        foreign authority would be served as though it named this host.
        """
        for target in ALIASED_REQUEST_TARGETS:
            with self.subTest(target=target):
                response = self._perform("GET", target)
                self.assertEqual(
                    response.status,
                    404,
                    f"{target!r} must not be an alias for {EXPECTED_PATH}",
                )
                self.assertEqual(response.json(), {"error": "Not Found"})
                self._assert_contract_headers(response)

    def test_the_absolute_form_of_this_servers_own_url_is_not_an_alias(self):
        """``GET http://127.0.0.1:<port>/health`` answers 404, correct host or not.

        The companion to the foreign-authority case in
        :data:`ALIASED_REQUEST_TARGETS`, and the more instructive one: the target
        names *this* server, so a parser that resolved it against a base URL would
        produce ``/health`` and serve it.  Origin form is the only form this
        endpoint accepts, so the answer is 404 either way -- which is what makes
        the rule "reject the absolute form" rather than "reject a foreign host".
        """
        response = self._perform(
            "GET", f"http://{self._host}:{self._port}{EXPECTED_PATH}"
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json(), {"error": "Not Found"})

    def test_the_alias_assertions_are_not_vacuous(self):
        """A control: the real path still answers 200, and no alias equals it.

        Without this, a handler that answered 404 to *everything* -- the endpoint
        broken outright -- would satisfy every assertion above.  The two halves are
        asserted in one test so they cannot drift apart.
        """
        self.assertEqual(self._perform("GET", EXPECTED_PATH).status, 200)
        self.assertEqual(self._perform("GET", f"{EXPECTED_PATH}?x=1").status, 200)
        self.assertNotIn(EXPECTED_PATH, ALIASED_REQUEST_TARGETS)
        self.assertEqual(
            len(set(ALIASED_REQUEST_TARGETS)), len(ALIASED_REQUEST_TARGETS)
        )

    def test_a_head_request_for_an_alias_is_also_a_404(self):
        """``HEAD`` follows the same routing as ``GET``: an alias is not the resource.

        Asserted separately because ``do_HEAD`` and ``do_GET`` are two entry points
        into the handler, and a routing rule enforced in only one of them would
        leave a cheap, body-less way to discover the alias.
        """
        for target in ALIASED_REQUEST_TARGETS:
            with self.subTest(target=target):
                response = self._perform("HEAD", target)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.body, b"")

    def test_an_alias_with_a_rejected_method_is_still_a_405(self):
        """``POST /other/../health`` answers 405: the method is still checked first.

        Ordering is part of the contract, and it must not depend on the shape of
        the target: a caller learns the most actionable fact -- that its method is
        not permitted anywhere on this server -- before anything about routing.
        """
        for target in ALIASED_REQUEST_TARGETS:
            with self.subTest(target=target):
                response = self._perform("POST", target)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)

    # -- Freshness ---------------------------------------------------------

    def test_three_leading_slashes_are_not_an_alias_either(self):
        """``GET ///health`` answers 404: the guard is not a two-slash special case."""
        response = self._perform("GET", f"//{EXPECTED_PATH}")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json(), {"error": "Not Found"})

    def test_the_raw_request_target_is_what_decides_the_route(self):
        """A hand-written ``GET //health`` request line is answered 404.

        Sent as raw bytes so there is no doubt about what crossed the wire: the
        assertion is that the *transmitted* target decides the route, not the value
        the standard library rewrote it into.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        connection.send(
            b"GET //health HTTP/1.1\r\n"
            b"Host: " + f"{self._host}:{self._port}".encode(EXPECTED_ENCODING) + b"\r\n"
            b"Connection: close\r\n\r\n"
        )
        response = connection.read_response()

        self.assertEqual(response.status, 404)
        self.assertEqual(response.json(), {"error": "Not Found"})

    # -- Request framing and parser safety ---------------------------------

    def test_a_rejected_request_with_a_body_leaves_the_connection_usable(self):
        """A drained body keeps a persistent connection correctly framed.

        Nothing legitimate sends a body to this endpoint, but a client that does
        must not desynchronize the connection: if the body were left unread, the
        next request would be parsed starting from the middle of it.  The proof is
        that a second, ordinary request on the *same* socket is answered correctly.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        authority = f"{self._host}:{self._port}".encode(EXPECTED_ENCODING)
        body = b'{"ignored":true}'
        connection.send(
            b"POST /health HTTP/1.1\r\nHost: " + authority + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode(EXPECTED_ENCODING) + b"\r\n\r\n" + body
        )
        rejected = connection.read_response(method="POST")

        self.assertEqual(rejected.status, 405)
        self.assertEqual(rejected.header("Allow"), EXPECTED_ALLOW)
        self.assertEqual(rejected.json(), {"error": "Method Not Allowed"})

        connection.send(b"GET /health HTTP/1.1\r\nHost: " + authority + b"\r\n\r\n")
        served = connection.read_response()

        self.assertEqual(served.status, 200, "the connection must still be framed correctly")
        self.assert_payload_conforms(served.json())

    def test_a_chunked_body_is_rejected_and_the_connection_is_closed(self):
        """A transfer-encoded body cannot be measured, so the connection ends.

        The response still has to be the contract's 405 -- the caller's method is
        the actionable fact -- but the connection cannot be reused, because how many
        bytes remain unread is unknowable.  Closing it is the safe answer.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        authority = f"{self._host}:{self._port}".encode(EXPECTED_ENCODING)
        connection.send(
            b"POST /health HTTP/1.1\r\nHost: " + authority + b"\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"4\r\nbody\r\n0\r\n\r\n"
        )
        response = connection.read_response(method="POST")

        self.assertEqual(response.status, 405)
        self.assertEqual(response.header("Allow"), EXPECTED_ALLOW)
        self.assertEqual(response.json(), {"error": "Method Not Allowed"})
        self.assertTrue(
            connection.is_closed_by_peer(),
            "an unmeasurable body must end the connection, not desynchronize it",
        )

    def test_an_unparseable_content_length_is_answered_then_closed(self):
        """A ``Content-Length`` that is not a number is not trusted."""
        connection = self._raw()
        self.addCleanup(connection.close)
        authority = f"{self._host}:{self._port}".encode(EXPECTED_ENCODING)
        connection.send(
            b"PUT /health HTTP/1.1\r\nHost: " + authority + b"\r\n"
            b"Content-Length: not-a-number\r\n\r\n"
        )
        response = connection.read_response(method="PUT")

        self.assertEqual(response.status, 405)
        self.assertEqual(response.json(), {"error": "Method Not Allowed"})
        self.assertTrue(connection.is_closed_by_peer())

    def test_an_oversized_declared_body_is_refused_without_being_read(self):
        """A declared length above the drain ceiling is refused, not waited for.

        No body is sent at all, so a server that trusted the declared length would
        block until its idle timeout.  The response therefore has to arrive
        promptly -- which the bounded read asserts by simply completing -- and the
        connection has to end.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        authority = f"{self._host}:{self._port}".encode(EXPECTED_ENCODING)
        declared = MAX_DRAIN_BYTES + 1
        connection.send(
            b"POST /health HTTP/1.1\r\nHost: " + authority + b"\r\n"
            b"Content-Length: " + str(declared).encode(EXPECTED_ENCODING) + b"\r\n\r\n"
        )
        response = connection.read_response(method="POST")

        self.assertEqual(response.status, 405)
        self.assertEqual(response.json(), {"error": "Method Not Allowed"})
        self.assertTrue(connection.is_closed_by_peer())

    def test_a_truncated_body_ends_the_connection_rather_than_hanging(self):
        """A peer that stops early is noticed, and the answer still arrives.

        The declared length is never satisfied and the write side is shut down, so
        the drain reads EOF instead of the promised bytes.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        authority = f"{self._host}:{self._port}".encode(EXPECTED_ENCODING)
        connection.send(
            b"POST /health HTTP/1.1\r\nHost: " + authority + b"\r\n"
            b"Content-Length: 512\r\n\r\ntruncated"
        )
        connection.half_close()
        response = connection.read_response(method="POST")

        self.assertEqual(response.status, 405)
        self.assertEqual(response.json(), {"error": "Method Not Allowed"})

    def test_a_malformed_request_line_is_answered_with_safe_compact_json(self):
        """A protocol-level 400 carries the contract's JSON shape, not HTML.

        The base class would answer with a half-kilobyte HTML page whose detail
        message quotes the offending request.  Both properties are wrong here: this
        server emits JSON and nothing else, and reflecting request bytes back to an
        unauthenticated caller is needless disclosure.  The assertion is therefore
        both positive (a compact single-member JSON body) and negative (no request
        bytes, no traceback, no HTML).

        The request line carries a fourth field, which the standard library rejects
        as bad syntax *after* it has accepted the version -- so this error is
        reported as a fully-formed HTTP response, unlike the case in the next test.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        connection.send(b"GET /health smuggled HTTP/1.1\r\n\r\n")
        response = connection.read_response()

        self.assertEqual(response.status, 400)
        self.assertEqual(response.header("Content-Type"), EXPECTED_CONTENT_TYPE)
        document = response.json()
        self.assertEqual(list(document), ["error"], "one member, named error")
        self.assertEqual(document["error"], "Bad Request", "the reason phrase, never the request")
        self.assertNotIn("smuggled", response.text(), "no request byte may be reflected")
        self.assertNotIn("Traceback", response.text())
        self.assertNotIn("<html", response.text().lower())
        self.assert_serialization_is_compact(response.text())

    def test_a_quoted_bad_version_cannot_produce_an_invalid_json_body(self):
        """The one error the base class would render as *invalid* JSON stays valid.

        This is the case the ``send_error`` override exists for.  The base class's
        detail message for an unparseable version embeds the version verbatim --
        ``Bad request version ('HTTP/1."bogus"')`` -- and the handler's error
        template is a JSON document, so interpolating a caller-supplied double
        quote into it would emit a body that no JSON parser accepts.  Dropping the
        detail and falling back to the static reason phrase is what keeps the body
        both valid and free of request bytes.

        The reply is read as raw bytes because the standard library has not yet
        determined the request version when it rejects this line, so it answers on
        the ``HTTP/0.9`` path: a bare body, no status line, nothing a client can
        parse.  That framing is inherited standard-library behaviour and is not
        what this test is about -- the body's *validity and safety* is.
        """
        connection = self._raw()
        self.addCleanup(connection.close)
        connection.send(b'GET /health HTTP/1."bogus"\r\n\r\n')
        raw = connection.read_until_close()

        text = raw.decode(EXPECTED_ENCODING)
        self.assertEqual(
            json.loads(text),
            {"error": "Bad Request"},
            "a caller-supplied quote must not be able to invalidate the body",
        )
        self.assertNotIn("bogus", text, "no request byte may be reflected")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("<html", text.lower())
        self.assert_serialization_is_compact(text)

    def test_an_over_long_request_target_is_answered_with_safe_compact_json(self):
        """A 414 is JSON too, and says nothing about the target that caused it."""
        connection = self._raw()
        self.addCleanup(connection.close)
        connection.send(b"GET /" + b"x" * 70000 + b" HTTP/1.1\r\n\r\n")
        response = connection.read_response()

        self.assertEqual(response.status, 414)
        self.assertEqual(response.header("Content-Type"), EXPECTED_CONTENT_TYPE)
        self.assertEqual(response.json(), {"error": "URI Too Long"})
        self.assertNotIn("xxxx", response.text(), "no part of the target may be echoed")
        self.assertNotIn("Traceback", response.text())

    # -- Freshness ---------------------------------------------------------
    # Freshness.

    def test_consecutive_requests_differ_only_in_the_timestamp(self):
        """Two back-to-back probes: a new timestamp, an identical identity.

        This is the wire-level form of the freshness clause, and it is what
        distinguishes a live process from a merely reachable one.  The two
        requests are issued with nothing between them -- no pause to manufacture a
        difference -- because that is how a real poller behaves when it wants an
        answer quickly, and the guarantee has to hold in exactly that case.
        """
        first = self._perform("GET", EXPECTED_PATH).json()
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


class _DegradedConfigurationFixture(unittest.TestCase):
    """Base class that runs ``health.py`` against a synthetic configuration.

    Every subclass assertion depends on being able to place arbitrary -- or
    absent, or broken -- configuration next to a copy of the module, without
    touching a single repository file.  ``_execute_module_source`` supplies the
    isolation; this class supplies the workspace and the guarantee that the real
    sources are unchanged afterwards.
    """

    def setUp(self):
        """Record the real sources' bytes and open an empty workspace."""
        self._identity_bytes = health.PYPROJECT_PATH.read_bytes()
        self._serving_bytes = health.CONFIG_PATH.read_bytes()
        workspace = tempfile.TemporaryDirectory(prefix="blitzy_health_degraded_")
        self.addCleanup(workspace.cleanup)
        self._workspace = pathlib.Path(workspace.name).resolve()
        self._loaded = 0

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

    def _load(self, identity=None, serving=None):
        """Execute a copy of ``health.py`` beside the given synthetic sources.

        :param identity: bytes to write as ``pyproject.toml``, or ``None`` to
            leave it absent.  The sentinel ``_AS_DIRECTORY`` creates a directory
            in its place, which is how an unreadable source is simulated without
            depending on file permissions -- those behave differently for a
            process running as root, which many automated environments do.
        :param serving: the same, for ``config/health.json``.
        :returns: the executed module.
        """
        self._loaded += 1
        root = self._workspace / f"case{self._loaded}"
        root.mkdir()
        copy = root / pathlib.Path(health.__file__).name
        copy.write_bytes(pathlib.Path(health.__file__).resolve().read_bytes())

        if identity is _AS_DIRECTORY:
            (root / "pyproject.toml").mkdir()
        elif identity is not None:
            (root / "pyproject.toml").write_bytes(identity)

        if serving is _AS_DIRECTORY:
            (root / "config").mkdir()
            (root / "config" / "health.json").mkdir()
        elif serving is not None:
            (root / "config").mkdir()
            (root / "config" / "health.json").write_bytes(serving)

        module, stdout_text, stderr_text = _execute_module_source(
            f"{_HEALTH_PROBE_MODULE_NAME}_{self._loaded}", copy
        )
        # The whole point of reporting from the entry point instead of the module
        # is that importing the module stays silent even when its configuration
        # is broken.  Asserted on every single load, because a regression here
        # would corrupt the output of any program that merely reads a constant.
        self.assertEqual(stdout_text, "", "importing health.py must write nothing")
        self.assertEqual(stderr_text, "", "importing health.py must write nothing")
        return module


class TestDegradedConfigurationSignaling(_DegradedConfigurationFixture):
    """A configuration fallback must be knowable, not merely survivable.

    The endpoint answers ``200`` with a complete, valid body whether its declared
    configuration loaded or not -- which is the correct behaviour and also the
    problem: nothing at the endpoint distinguishes declared identity from
    fallback identity, so a deployment that did not ship its configuration file
    looks perfectly healthy while serving values nobody declared.

    These tests pin the mechanism that closes that gap, and equally importantly
    pin what it must never disclose.
    """

    def test_the_real_repository_reports_no_degradation(self):
        """Both declared sources load, so there is nothing to report."""
        self.assertEqual(health.CONFIG_DEGRADATIONS, ())
        self.assertIsNone(health.describe_configuration_degradation())

    def test_absent_sources_are_reported_as_missing(self):
        """No configuration at all names both sources, and says why."""
        module = self._load()
        self.assertEqual(
            module.CONFIG_DEGRADATIONS,
            (
                f"{module.SOURCE_IDENTITY}: {module.DEGRADED_MISSING}",
                f"{module.SOURCE_SERVING}: {module.DEGRADED_MISSING}",
            ),
        )

    def test_unparseable_sources_are_reported_as_malformed(self):
        """A syntax error in either source is a distinct, named condition."""
        module = self._load(
            identity=b"this is not = = valid toml [[[",
            serving=b"{not json at all",
        )
        self.assertEqual(
            module.CONFIG_DEGRADATIONS,
            (
                f"{module.SOURCE_IDENTITY}: {module.DEGRADED_MALFORMED}",
                f"{module.SOURCE_SERVING}: {module.DEGRADED_MALFORMED}",
            ),
        )

    def test_a_valid_document_of_the_wrong_shape_is_malformed(self):
        """Valid JSON that is not an object cannot be looked up in.

        Reported as malformed rather than incomplete: no amount of key lookup
        could succeed against a list, so the file is structurally wrong for its
        purpose rather than merely short of a value.
        """
        module = self._load(
            identity=b'[project]\nname = "n"\nversion = "1.2.3"\n',
            serving=b'["a", "list", "not", "an", "object"]',
        )
        self.assertEqual(
            module.CONFIG_DEGRADATIONS,
            (f"{module.SOURCE_SERVING}: {module.DEGRADED_MALFORMED}",),
        )
        # The identity source loaded, so its values -- not the literals -- are used.
        self.assertEqual(module.APP_NAME, "n")
        self.assertEqual(module.APP_VERSION, "1.2.3")

    def test_a_parseable_source_missing_its_values_is_incomplete(self):
        """Valid TOML without a ``[project]`` table parsed but yielded nothing."""
        module = self._load(identity=b'[build-system]\nrequires = ["setuptools"]\n')
        self.assertIn(
            f"{module.SOURCE_IDENTITY}: {module.DEGRADED_INCOMPLETE}",
            module.CONFIG_DEGRADATIONS,
        )

    def test_an_unopenable_source_is_reported_as_unreadable(self):
        """A directory where a file belongs is distinguished from absence."""
        module = self._load(identity=_AS_DIRECTORY, serving=_AS_DIRECTORY)
        self.assertEqual(
            module.CONFIG_DEGRADATIONS,
            (
                f"{module.SOURCE_IDENTITY}: {module.DEGRADED_UNREADABLE}",
                f"{module.SOURCE_SERVING}: {module.DEGRADED_UNREADABLE}",
            ),
        )

    def test_only_the_source_that_failed_is_reported(self):
        """A working identity source is not implicated by a broken serving one."""
        module = self._load(
            identity=b'[project]\nname = "declared"\nversion = "4.5.6"\n'
        )
        self.assertEqual(
            module.CONFIG_DEGRADATIONS,
            (f"{module.SOURCE_SERVING}: {module.DEGRADED_MISSING}",),
        )
        self.assertNotIn(module.SOURCE_IDENTITY, "".join(module.CONFIG_DEGRADATIONS))

    def test_the_rendered_line_is_single_and_self_contained(self):
        """One line, naming both the condition and the consequence."""
        module = self._load()
        line = module.describe_configuration_degradation()
        self.assertIsNotNone(line)
        self.assertEqual(len(line.splitlines()), 1, "a diagnostic must be one line")
        self.assertTrue(line.startswith(module._DEGRADED_PREFIX))
        self.assertTrue(line.endswith(module._DEGRADED_SUFFIX))
        for reason in module.CONFIG_DEGRADATIONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, line)

    def test_the_diagnostic_discloses_no_configuration_data(self):
        """The text is built from constants, so it cannot leak what it read.

        This is the property that makes the diagnostic safe to emit into any log:
        an operator learns the category, and nothing about the filesystem, the
        file's contents, or the environment.
        """
        secret = b'[project]\nname = "s3cr3t-value"\nversion = "1.0.0"\nx = "/etc/shadow"\n'
        module = self._load(identity=secret, serving=b"{ broken")
        rendered = " ".join(
            (module.describe_configuration_degradation(),) + module.CONFIG_DEGRADATIONS
        )
        for forbidden in (
            "s3cr3t-value",
            "/etc/shadow",
            "pyproject",
            "health.json",
            str(self._workspace),
            "/",
            "\\",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)
        self.assertTrue(
            all(character.isprintable() or character == " " for character in rendered),
            "a diagnostic must not carry a control character into a log",
        )

    def test_the_contract_still_holds_while_degraded(self):
        """Reporting the fallback must not change what the fallback serves.

        The endpoint's whole value during a misconfiguration is that it still
        answers, so the four-member contract is re-asserted here rather than
        assumed.
        """
        module = self._load(identity=_AS_DIRECTORY, serving=b"{ broken")
        self.assertNotEqual(module.CONFIG_DEGRADATIONS, ())
        payload = module.build_payload()
        self.assertEqual(list(payload), EXPECTED_MEMBERS)
        self.assertEqual(payload["name"], EXPECTED_NAME)
        self.assertEqual(payload["version"], EXPECTED_VERSION)
        self.assertEqual(payload["status"], EXPECTED_STATUS)
        self.assertRegex(payload["timestamp"], TIMESTAMP_PATTERN)
        serialized = module.serialize(payload).decode(EXPECTED_ENCODING)
        self.assertTrue(serialized.startswith(SERIALIZED_PREFIX))
        self.assertTrue(serialized.endswith(SERIALIZED_SUFFIX))
        for marker in DEFAULT_SEPARATOR_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, serialized)


class TestHandlerFailureReporting(_DegradedConfigurationFixture):
    """A client that disconnects is not a defect; a defect must not look like one.

    A poller that hangs up mid-response and a genuine fault inside the handler
    both end the same way on the wire -- the connection simply closes -- and since
    the contract defines no ``5xx``, neither is distinguishable from the endpoint
    either.  So the two must be told apart *before* the connection is abandoned,
    and only the second may be reported: a fault that appears nowhere is a fault
    nobody fixes, while a logged line per aborted poll is noise that hides real
    ones.  These tests pin that classifier in both directions.

    An isolated module copy is used throughout, so the reporting latch these
    tests exercise belongs to the copy and the real module's state is never
    touched.
    """

    def setUp(self):
        """Load an isolated module and a handler that needs no socket."""
        super().setUp()
        self.module = self._load(
            identity=b'[project]\nname = "child_repo_10_LOC"\nversion = "1.0.0"\n'
        )

        class _SocketlessHandler(self.module.HealthRequestHandler):
            """The handler with its ``BaseHTTPRequestHandler`` setup bypassed.

            ``BaseHTTPRequestHandler.__init__`` immediately reads a request from a
            socket.  These assertions are about the failure classifier, not about
            request parsing, so construction is skipped and only the attributes
            the responders actually reach are provided: ``close_connection`` for
            the classifier, and ``requestline``/``request_version`` because
            ``send_response`` records the request line before it writes.
            """

            def __init__(self):
                self.close_connection = False
                self.requestline = f"GET {EXPECTED_PATH} HTTP/1.1"
                self.request_version = "HTTP/1.1"

        self.handler = _SocketlessHandler()

    @contextlib.contextmanager
    def _captured_stderr(self):
        """Yield a buffer receiving everything the handler writes."""
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            yield buffer

    def test_a_peer_disconnect_is_never_reported(self):
        """Every transport failure stays silent, and still closes the connection.

        A poller that stops waiting is normal and frequent.  Reporting it would
        put a line in the log per aborted probe, which is the per-request noise
        the contract forbids.
        """
        expected = (
            BrokenPipeError(32, "Broken pipe"),
            ConnectionResetError(104, "Connection reset by peer"),
            ConnectionAbortedError(103, "Software caused connection abort"),
            TimeoutError(),
            OSError(9, "Bad file descriptor"),
            None,
        )
        with self._captured_stderr() as buffer:
            for error in expected:
                self.handler.close_connection = False
                self.handler._abandon_connection(error)
                self.assertTrue(
                    self.handler.close_connection,
                    "the connection must be closed on every path",
                )
        self.assertEqual(buffer.getvalue(), "")
        self.assertEqual(self.module.reported_handler_failures(), frozenset())

    def test_an_internal_defect_is_reported_once_per_category(self):
        """The first occurrence speaks; every repetition is silent."""
        with self._captured_stderr() as buffer:
            for _ in range(5):
                self.handler._abandon_connection(ZeroDivisionError("division by zero"))
            for _ in range(3):
                self.handler._abandon_connection(KeyError("some-internal-detail"))
        lines = buffer.getvalue().splitlines()
        self.assertEqual(
            len(lines), 2, "eight failures in two categories must yield two lines"
        )
        self.assertEqual(
            self.module.reported_handler_failures(),
            frozenset({"ZeroDivisionError", "KeyError"}),
        )
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(line.startswith(self.module.HANDLER_FAILURE_PREFIX))
                self.assertTrue(line.endswith(self.module.HANDLER_FAILURE_SUFFIX))

    def test_the_report_discloses_nothing_about_the_exception(self):
        """Only the category is written -- never the exception's own message.

        An exception message is the most likely place for a value that should not
        be in a log: a key, a path, a fragment of a request.  The category is the
        actionable part and is all that is emitted.
        """
        with self._captured_stderr() as buffer:
            self.handler._abandon_connection(
                ValueError("token=abcdef12345 while reading /etc/shadow")
            )
        rendered = buffer.getvalue()
        self.assertIn("ValueError", rendered)
        for forbidden in ("token=", "abcdef12345", "/etc/shadow"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_the_number_of_reported_categories_is_bounded(self):
        """An adversarial variety of failures cannot grow the latch without limit."""
        with self._captured_stderr() as buffer:
            for index in range(self.module.MAX_REPORTED_HANDLER_FAILURES * 3):
                error_type = type(f"Synthetic{index}", (Exception,), {})
                self.handler._abandon_connection(error_type())
        self.assertEqual(
            len(self.module.reported_handler_failures()),
            self.module.MAX_REPORTED_HANDLER_FAILURES,
        )
        self.assertEqual(
            len(buffer.getvalue().splitlines()),
            self.module.MAX_REPORTED_HANDLER_FAILURES,
        )

    def test_a_hostile_category_cannot_forge_a_log_line(self):
        """A class name carrying a newline is stripped, not printed.

        Far-fetched by design: the point is that the sink is safe by construction
        rather than by the good behaviour of its callers.
        """
        hostile = type(
            "Bad\nhealth configuration degraded: FORGED", (Exception,), {}
        )
        with self._captured_stderr() as buffer:
            self.handler._abandon_connection(hostile())
        lines = buffer.getvalue().splitlines()
        self.assertEqual(len(lines), 1, "one failure must produce exactly one line")
        self.assertNotIn("FORGED:", lines[0])
        self.assertTrue(all(character.isprintable() for character in lines[0]))

    def test_the_dispatcher_still_closes_without_reporting_a_disconnect(self):
        """``_send`` funnels an ``OSError`` to the quiet path.

        Exercised through the real responder rather than the classifier directly,
        so the wiring is covered and not just the decision.
        """

        class _BrokenStream:
            """A response stream whose every write fails as a dead peer would."""

            def write(self, data):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

        self.handler.wfile = _BrokenStream()
        self.handler.request_version = "HTTP/1.1"
        with self._captured_stderr() as buffer:
            self.handler._send(self.module.STATUS_OK, b"{}", write_body=True)
        self.assertTrue(self.handler.close_connection)
        self.assertEqual(buffer.getvalue(), "")
        self.assertEqual(self.module.reported_handler_failures(), frozenset())


class TestBindDiagnosticSanitization(unittest.TestCase):
    """A diagnostic must not become a channel for the value it is describing.

    The host reaching a bind diagnostic comes from ``HEALTH_HOST`` or from
    ``config/health.json``: configuration-controlled text, written into a log
    that a human or a parser later trusts.  One newline in it is enough to forge
    a whole additional entry.

    ``server.py`` is imported here -- safely, because binding happens in
    ``main()`` and never at import -- so these assertions run against the real
    module without a socket ever being created.
    """

    def test_importing_the_entry_point_is_silent(self):
        """Importing ``server.py`` must not write, and must not bind."""
        module, stdout_text, stderr_text = _execute_module_source(
            _SERVER_PROBE_MODULE_NAME, pathlib.Path(server.__file__).resolve()
        )
        self.assertEqual(stdout_text, "")
        self.assertEqual(stderr_text, "")
        self.assertEqual(module.DEFAULT_PORT, TIER_PORT)

    def test_a_legitimate_host_is_rendered_unchanged(self):
        """Every form a real bind address takes survives sanitization."""
        for host, expected in (
            (EXPECTED_BIND_HOST, f"{EXPECTED_BIND_HOST}:{TIER_PORT}"),
            (LOOPBACK_HOST, f"{LOOPBACK_HOST}:{TIER_PORT}"),
            ("localhost", f"localhost:{TIER_PORT}"),
            ("my-host_1.example.com", f"my-host_1.example.com:{TIER_PORT}"),
            ("::1", f"[::1]:{TIER_PORT}"),
            ("::", f"[::]:{TIER_PORT}"),
            ("fe80::1%eth0", f"[fe80::1%eth0]:{TIER_PORT}"),
        ):
            with self.subTest(host=host):
                self.assertEqual(server._format_authority(host, TIER_PORT), expected)

    def test_an_unrenderable_host_is_replaced(self):
        """Anything that is not a host is replaced rather than escaped.

        A value that cannot be bound has no diagnostic value in its exact bytes;
        what an operator needs to know is that the configured value was not a
        host, which is what the placeholder says.
        """
        hostile = (
            "evil\nserver.py: listening on http://0.0.0.0:8000/health",
            "carriage\rreturn",
            "with space",
            "with\ttab",
            "\x1b[31mansi",
            "\x00null",
            "x" * 512,
            "",
            None,
            12345,
            ("tuple",),
        )
        for host in hostile:
            with self.subTest(host=repr(host)[:40]):
                rendered = server._format_authority(host, TIER_PORT)
                self.assertEqual(
                    rendered, f"{server.UNSAFE_HOST_TEXT}:{TIER_PORT}"
                )

    def test_a_bind_diagnostic_emits_exactly_the_lines_it_was_given(self):
        """A newline in the host cannot add a third line to a two-line report."""
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            server._report_bind_failure(
                "attacker\nserver.py: listening on http://0.0.0.0:8000/health",
                TIER_PORT,
                OSError(errno.EADDRINUSE, "Address already in use"),
            )
        lines = buffer.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("listening on", buffer.getvalue())
        self.assertIn(server.UNSAFE_HOST_TEXT, lines[0])

    def test_error_categories_are_stable_symbols_not_os_prose(self):
        """The unmapped branch names an errno symbol, never ``strerror``.

        ``strerror`` is locale-dependent and, for several errors, interpolates
        the address or filename that failed -- so it makes the diagnostic both
        unstable across hosts and capable of echoing configuration into the log.
        """
        self.assertEqual(
            server._error_category(OSError(errno.EADDRINUSE, "x")), "EADDRINUSE"
        )
        self.assertEqual(
            server._error_category(OSError(errno.EACCES, "x")), "EACCES"
        )
        self.assertEqual(server._error_category(RuntimeError("x")), "RuntimeError")
        self.assertEqual(server._error_category(OSError()), "OSError")

        unmapped = OSError(errno.EPROTOTYPE, "Protocol wrong type -- /var/run/x.sock")
        reason, remedy = server._describe_bind_failure(TIER_PORT, unmapped)
        self.assertIn("EPROTOTYPE", reason)
        self.assertNotIn("Protocol wrong type", reason)
        self.assertNotIn("/var/run/x.sock", reason)
        self.assertTrue(remedy)

    def test_every_named_bind_condition_names_its_remedy(self):
        """A diagnostic that does not say what to do next is half a diagnostic."""
        for error, port in (
            (OSError(errno.EADDRINUSE, "in use"), TIER_PORT),
            (OSError(errno.EACCES, "denied"), 80),
            (OSError(errno.EACCES, "denied"), 9999),
            (OSError(errno.EADDRNOTAVAIL, "not available"), TIER_PORT),
            (socket.gaierror(-2, "unknown"), TIER_PORT),
            (OSError(errno.EPROTOTYPE, "other"), TIER_PORT),
        ):
            with self.subTest(errno=getattr(error, "errno", None), port=port):
                reason, remedy = server._describe_bind_failure(port, error)
                self.assertTrue(reason and remedy)
                self.assertEqual(len(reason.splitlines()), 1)
                self.assertIn(
                    health.ENV_PORT if "PORT" in remedy else health.ENV_HOST, remedy
                )


class TestShutdownLifecycleStatus(unittest.TestCase):
    """A shutdown that failed must not be reported as a success.

    The failure matters operationally rather than cosmetically: a failed
    ``shutdown()`` means the accept loop may still be running and the port may
    still be held, so a supervisor that reads exit 0 and restarts immediately
    meets a confusing bind failure instead of the real cause.
    """

    class _StubServer:
        """The smallest object ``serve_until_signalled`` can drive.

        ``serve_forever`` returns at once, which puts the function straight into
        its shutdown path -- the only part under test here.
        """

        def __init__(self, on_shutdown=None):
            self.on_shutdown = on_shutdown
            self.closed = False

        def serve_forever(self, poll_interval=None):
            return None

        def shutdown(self):
            if self.on_shutdown is not None:
                self.on_shutdown()

        def server_close(self):
            self.closed = True

    def _drive(self, stub):
        """Run the lifecycle against ``stub``, returning ``(status, lines)``."""
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            status = server.serve_until_signalled(stub)
        return status, buffer.getvalue().splitlines()

    def test_a_clean_shutdown_is_silent_and_successful(self):
        """The ordinary path keeps the process's output to its one start-up line."""
        stub = self._StubServer()
        status, lines = self._drive(stub)
        self.assertEqual(status, server.EXIT_OK)
        self.assertEqual(lines, [])
        self.assertTrue(stub.closed, "the socket is released on every path")

    def test_a_failing_shutdown_reports_and_fails(self):
        """One sanitized line, a stable category, and a non-zero status."""

        def raise_oserror():
            raise OSError(errno.EPIPE, "Broken pipe -- /var/run/health.sock")

        stub = self._StubServer(raise_oserror)
        status, lines = self._drive(stub)
        self.assertEqual(status, server.EXIT_SHUTDOWN_FAILURE)
        self.assertNotEqual(status, server.EXIT_OK)
        self.assertEqual(len(lines), 1)
        self.assertIn("EPIPE", lines[0])
        self.assertNotIn("Broken pipe", lines[0])
        self.assertNotIn("/var/run/health.sock", lines[0])
        self.assertTrue(stub.closed)

    def test_a_non_oserror_shutdown_failure_is_categorized_by_class(self):
        """Any exception type is reported, and none of its text is."""

        def raise_runtime():
            raise RuntimeError("internal detail that must not be logged")

        status, lines = self._drive(self._StubServer(raise_runtime))
        self.assertEqual(status, server.EXIT_SHUTDOWN_FAILURE)
        self.assertIn("RuntimeError", lines[0])
        self.assertNotIn("internal detail", lines[0])

    def test_a_shutdown_that_never_returns_is_a_failure_and_is_bounded(self):
        """A hung ``shutdown()`` fails the status without hanging the process.

        The helper is a daemon thread and cannot keep the interpreter alive, but
        an expired join means the accept loop was never confirmed stopped -- a
        failed shutdown by any useful definition.
        """
        original = server._SHUTDOWN_JOIN_TIMEOUT
        server._SHUTDOWN_JOIN_TIMEOUT = 0.25
        self.addCleanup(setattr, server, "_SHUTDOWN_JOIN_TIMEOUT", original)

        started = threading.Event()

        def hang():
            started.set()
            time.sleep(LISTENER_JOIN_TIMEOUT_SECONDS * 10)

        stub = self._StubServer(hang)
        began = time.monotonic()
        status, lines = self._drive(stub)
        elapsed = time.monotonic() - began

        self.assertTrue(started.wait(timeout=REQUEST_TIMEOUT_SECONDS))
        self.assertEqual(status, server.EXIT_SHUTDOWN_FAILURE)
        self.assertEqual(len(lines), 1)
        self.assertIn("Timeout", lines[0])
        self.assertTrue(stub.closed)
        self.assertLess(
            elapsed,
            LISTENER_JOIN_TIMEOUT_SECONDS,
            "a hung shutdown must not become a hung process",
        )

    def test_a_real_listener_shut_down_on_request_still_succeeds(self):
        """The success path is asserted against a real socket, not only a stub.

        Binding an ephemeral loopback port rather than the tier's own, so this
        test cannot collide with -- or silently probe -- a running server.
        """
        listener = server.create_server(LOOPBACK_HOST, EPHEMERAL_PORT)
        self.assertGreater(listener.server_address[1], 0)

        def stop_shortly():
            time.sleep(SHUTDOWN_REQUEST_DELAY_SECONDS)
            listener.shutdown()

        threading.Thread(target=stop_shortly, daemon=True).start()
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            status = server.serve_until_signalled(listener)
        self.assertEqual(status, server.EXIT_OK)
        self.assertEqual(buffer.getvalue(), "")
class TestServerHelpers(unittest.TestCase):
    """``server.py``'s pure helpers: validation, formatting and diagnostics.

    These functions decide what the entry point binds and what it says when it
    cannot.  They take no sockets and no environment, so the whole matrix is
    stated here directly, leaving the process-level classes below to prove only
    what genuinely needs a process.
    """

    def test_the_declared_defaults_are_re_exported_not_restated(self):
        """The tier's defaults live in ``health.py`` and are read from there.

        If these were spelled again in ``server.py``, "the default port is 8000"
        would have two homes and one of them would eventually be wrong.
        """
        self.assertEqual(server.DEFAULT_HOST, health.FALLBACK_HOST)
        self.assertEqual(server.DEFAULT_PORT, health.FALLBACK_PORT)
        self.assertEqual(server.DEFAULT_HOST, EXPECTED_BIND_HOST)
        self.assertEqual(server.DEFAULT_PORT, TIER_PORT)

    def test_the_process_level_constants_are_what_the_contract_states(self):
        """Exit statuses and shutdown signals are named, not magic numbers."""
        self.assertEqual(server.EXIT_OK, 0)
        self.assertEqual(server.EXIT_BIND_FAILURE, 1)
        self.assertEqual(server.SHUTDOWN_SIGNALS, (signal.SIGTERM, signal.SIGINT))

    def test_the_public_surface_is_exactly_what_is_declared(self):
        """Every name in ``__all__`` exists, and the listener class is among them."""
        for name in server.__all__:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(server, name), f"__all__ names {name}, which does not exist"
                )
        self.assertIn("HealthHTTPServer", server.__all__)
        self.assertIn("main", server.__all__)
        self.assertEqual(len(server.__all__), len(set(server.__all__)), "no duplicates")

    def test_only_a_non_blank_string_is_a_usable_host(self):
        """A blank or wrongly-typed host means "not supplied", never an empty bind."""
        for candidate, expected in (
            ("127.0.0.1", "127.0.0.1"),
            ("  0.0.0.0  ", "0.0.0.0"),
            ("::1", "::1"),
            ("", None),
            ("   ", None),
            (None, None),
            (8000, None),
            (True, None),
            ([], None),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(server._usable_host(candidate), expected)

    def test_only_an_in_range_port_is_a_usable_port(self):
        """Bounds are inclusive, ``0`` is admitted, and a boolean is refused."""
        for candidate, expected in (
            (8000, 8000),
            (0, 0),
            (65535, 65535),
            ("8000", 8000),
            ("  0  ", 0),
            (-1, None),
            (65536, None),
            ("abc", None),
            ("", None),
            (None, None),
            (True, None),
            (False, None),
            (8000.0, None),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(server._usable_port(candidate), expected)

    def test_an_ipv6_literal_selects_the_ipv6_family(self):
        """``HTTPServer`` is hard-wired to IPv4, so the family is chosen explicitly."""
        for host, expected in (
            ("0.0.0.0", socket.AF_INET),
            ("127.0.0.1", socket.AF_INET),
            ("localhost", socket.AF_INET),
            ("::1", socket.AF_INET6),
            ("::", socket.AF_INET6),
            ("fe80::1", socket.AF_INET6),
        ):
            with self.subTest(host=host):
                self.assertEqual(server._address_family(host), expected)

    def test_an_ipv6_authority_is_bracketed(self):
        """RFC 3986 form, so the logged address can be pasted into a client."""
        self.assertEqual(server._format_authority("127.0.0.1", 8000), "127.0.0.1:8000")
        self.assertEqual(server._format_authority("0.0.0.0", 8000), "0.0.0.0:8000")
        self.assertEqual(server._format_authority("::1", 8080), "[::1]:8080")
        self.assertEqual(server._format_authority("::", 8080), "[::]:8080")

    def test_an_explicit_argument_beats_the_resolved_configuration(self):
        """Link one of the entry point's ordering: the caller's own request wins.

        This is how a test asks for the loopback interface and an ephemeral port
        without touching the process environment.
        """
        self.assertEqual(
            server.resolve_bind_address(LOOPBACK_HOST, EPHEMERAL_PORT),
            (LOOPBACK_HOST, EPHEMERAL_PORT),
        )

    def test_an_absent_argument_falls_through_to_the_resolved_configuration(self):
        """Link two: what ``health.py`` resolved at import time."""
        self.assertEqual(
            server.resolve_bind_address(), (health.HOST, health.PORT)
        )
        self.assertEqual(
            server.resolve_bind_address(None, EPHEMERAL_PORT),
            (health.HOST, EPHEMERAL_PORT),
        )
        self.assertEqual(
            server.resolve_bind_address(LOOPBACK_HOST, None),
            (LOOPBACK_HOST, health.PORT),
        )

    def test_an_unusable_argument_falls_through_rather_than_raising(self):
        """A caller's typo degrades to the next link; it never stops the endpoint."""
        for host, port in (("", "abc"), ("   ", 70000), (None, True), (8000, -1)):
            with self.subTest(host=host, port=port):
                resolved_host, resolved_port = server.resolve_bind_address(host, port)
                self.assertEqual(resolved_host, health.HOST)
                self.assertEqual(resolved_port, health.PORT)

    def test_resolve_bind_address_always_returns_a_directly_usable_pair(self):
        """The contract of the return value: a non-blank ``str`` and an in-range ``int``."""
        for host, port in ((None, None), ("", ""), (LOOPBACK_HOST, 0), (8000, 8000.0)):
            with self.subTest(host=host, port=port):
                resolved_host, resolved_port = server.resolve_bind_address(host, port)
                self.assertIsInstance(resolved_host, str)
                self.assertTrue(resolved_host.strip())
                self.assertIsInstance(resolved_port, int)
                self.assertNotIsInstance(resolved_port, bool)
                self.assertGreaterEqual(resolved_port, 0)
                self.assertLessEqual(resolved_port, 65535)

    def test_the_startup_line_names_the_address_the_path_and_the_application(self):
        """One line, no trailing newline, in the documented form."""
        line = server.startup_line("0.0.0.0", 8000)
        self.assertEqual(
            line,
            f"listening on http://0.0.0.0:8000{EXPECTED_PATH}"
            f" ({EXPECTED_NAME} {EXPECTED_VERSION})",
        )
        self.assertNotIn("\n", line)

    def test_the_startup_line_reports_the_port_it_is_given(self):
        """An ephemeral port is announced as the number the kernel chose, not as 0."""
        self.assertIn(":54321", server.startup_line(LOOPBACK_HOST, 54321))
        self.assertIn("[::1]:8080", server.startup_line("::1", 8080))

    def test_the_startup_line_accepts_an_explicit_path(self):
        """The path is a parameter, so the line cannot drift from what is served."""
        self.assertIn("/probe", server.startup_line(LOOPBACK_HOST, 1, "/probe"))

    def test_an_occupied_port_is_described_with_a_remedy(self):
        """``EADDRINUSE`` names the condition and the variable that fixes it."""
        reason, remedy = server._describe_bind_failure(
            8000, OSError(errno.EADDRINUSE, "Address already in use")
        )
        self.assertEqual(reason, "address already in use")
        self.assertIn(health.ENV_PORT, remedy)
        self.assertIn("python server.py", remedy)

    def test_a_privileged_port_is_distinguished_from_a_refused_one(self):
        """The same errno means two different things either side of port 1024."""
        privileged_reason, privileged_remedy = server._describe_bind_failure(
            80, OSError(errno.EACCES, "Permission denied")
        )
        self.assertEqual(privileged_reason, "permission denied")
        self.assertIn("privileged", privileged_remedy)
        self.assertIn(f"{health.ENV_PORT}={server.DEFAULT_PORT}", privileged_remedy)

        refused_reason, refused_remedy = server._describe_bind_failure(
            9000, OSError(errno.EPERM, "Operation not permitted")
        )
        self.assertEqual(refused_reason, "permission denied")
        self.assertNotIn("privileged", refused_remedy)
        self.assertIn(health.ENV_PORT, refused_remedy)

    def test_an_unavailable_address_points_at_the_host_variable(self):
        """``EADDRNOTAVAIL`` is a host problem, so the remedy names the host variable."""
        reason, remedy = server._describe_bind_failure(
            8000, OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        )
        self.assertEqual(reason, "address not available on this host")
        self.assertIn(health.ENV_HOST, remedy)
        self.assertIn(server.DEFAULT_HOST, remedy)

    def test_an_unresolvable_host_is_described_as_such(self):
        """``gaierror`` is not an errno case, so it is matched by type."""
        reason, remedy = server._describe_bind_failure(8000, socket.gaierror("no name"))
        self.assertEqual(reason, "host could not be resolved")
        self.assertIn(health.ENV_HOST, remedy)

    def test_an_unrecognized_failure_reports_a_stable_errno_category(self):
        """The fallback branch names the errno, never the platform's own wording.

        ``strerror`` is locale-dependent and, for some errors, interpolates the
        address that failed, so quoting it would make the diagnostic both
        unstable across hosts and capable of echoing configuration into the log.
        The symbolic errno name is the greppable part and is identical
        everywhere.
        """
        reason, remedy = server._describe_bind_failure(
            8000, OSError(errno.ENOBUFS, "No buffer space available")
        )
        self.assertEqual(reason, "bind failed (ENOBUFS)")
        self.assertNotIn("No buffer space available", reason)
        self.assertIn(health.ENV_HOST, remedy)
        self.assertIn(health.ENV_PORT, remedy)

    def test_a_failure_with_no_wording_at_all_still_produces_a_sentence(self):
        """An ``OSError`` carrying neither errno nor strerror is still reportable."""
        reason, remedy = server._describe_bind_failure(8000, OSError())
        self.assertTrue(reason)
        self.assertTrue(remedy)

    def test_the_bind_diagnostic_is_two_prefixed_lines_and_no_traceback(self):
        """Both lines name the program, and the first names the authority."""
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            server._report_bind_failure(
                LOOPBACK_HOST, 8000, OSError(errno.EADDRINUSE, "Address already in use")
            )
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2, "two lines, never a traceback")
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(line.startswith("server.py: "))
        self.assertIn(f"cannot bind {LOOPBACK_HOST}:8000", lines[0])
        self.assertIn("address already in use", lines[0])
        self.assertNotIn("Traceback", stream.getvalue())

    def test_reporting_a_problem_never_becomes_the_problem(self):
        """A standard error that fails to accept the write is swallowed, not raised.

        The reachable condition is that the far end of the inherited pipe has gone
        away -- an orchestrator that stopped reading the log, a shell that exited --
        which surfaces as ``BrokenPipeError`` or a bare ``OSError``.  Either one
        escaping would replace the exit status the caller is about to return with
        an unhandled exception, turning a legible "cannot bind" into a traceback.

        A stream double is used rather than a closed :class:`io.StringIO`, because
        a closed Python stream raises ``ValueError`` -- a condition this entry point
        cannot reach, since nothing in it ever closes standard error.  Asserting
        against the reachable failure is what makes this test meaningful.
        """

        class _FailingStream:
            """A stream whose every write fails the way a dead pipe does."""

            def __init__(self, error):
                """Remember the error to raise, and count the attempts."""
                self.error = error
                self.attempts = 0

            def write(self, _text):
                """Fail, as a pipe with no reader does."""
                self.attempts += 1
                raise self.error

            def flush(self):
                """Accept the flush: ``print`` issues one after a successful write."""

        for error in (
            BrokenPipeError(errno.EPIPE, "Broken pipe"),
            OSError(errno.EBADF, "Bad file descriptor"),
        ):
            with self.subTest(error=type(error).__name__):
                stream = _FailingStream(error)
                with contextlib.redirect_stderr(stream):
                    server._write_stderr(["first", "second"])
                self.assertEqual(
                    stream.attempts,
                    1,
                    "reporting stops at the first failure instead of retrying",
                )

    def test_a_working_stderr_receives_every_line(self):
        """The companion to the failure case: nothing is dropped when writing works."""
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            server._write_stderr(["first", "second", "third"])
        self.assertEqual(
            stream.getvalue().splitlines(),
            ["server.py: first", "server.py: second", "server.py: third"],
        )


class TestServerListenerLifecycle(PayloadContractAssertions, unittest.TestCase):
    """``create_server`` and ``serve_until_signalled``, in process, on an ephemeral port.

    Every listener here is bound to ``127.0.0.1`` on an ephemeral port and closed
    by a registered cleanup, so the tier's port 8000 is never taken and nothing
    survives a failure.  The point of these cases is the two behaviours that only
    a real listener can demonstrate: that the socket is genuinely bound and
    serving before anything is served, and that shutting it down releases the port
    rather than merely silencing it.
    """

    def _bind(self, host=LOOPBACK_HOST, port=EPHEMERAL_PORT):
        """Bind a listener with a guaranteed close, and return it with its address."""
        listener = server.create_server(host, port)
        self.addCleanup(listener.server_close)
        bound_host, bound_port = listener.server_address[:2]
        self.assertNotEqual(bound_port, TIER_PORT, "a test must never bind port 8000")
        self.assertNotEqual(bound_port, EPHEMERAL_PORT)
        return listener, bound_host, bound_port

    def test_the_listener_is_configured_for_prompt_orderly_shutdown(self):
        """The four class attributes an orderly stop depends on."""
        listener, _, _ = self._bind()
        self.assertTrue(listener.daemon_threads, "a handler must not outlive the process")
        self.assertFalse(
            listener.block_on_close, "closing must not wait on in-flight handlers"
        )
        self.assertTrue(listener.allow_reuse_address, "an immediate rebind must work")
        self.assertEqual(listener.address_family, socket.AF_INET)
        self.assertIsInstance(listener, server.HealthHTTPServer)
        self.assertIsInstance(listener, ThreadingHTTPServer)

    def test_the_handler_and_server_classes_are_substitutable(self):
        """Both are documented parameters, so both are proven to be honoured.

        A subclass of the contract handler is the only sensible substitution -- the
        contract is defined by that class -- and this proves a caller wanting to
        observe or extend it does not have to reimplement the binding logic.
        """

        class _CountingHandler(health.HealthRequestHandler):
            """The contract handler, counting the requests it answers."""

            answered = 0

            def do_GET(self):
                """Answer exactly as the contract requires, and record the call.

                The upper-case verb in the method name is
                :class:`~http.server.BaseHTTPRequestHandler`'s dispatch
                convention, not a naming choice this suite is free to make.
                """
                type(self).answered += 1
                super().do_GET()

        class _RecordingServer(server.HealthHTTPServer):
            """The tier's listener, recording the address it was asked to bind."""

            requested = None

            def __init__(self, address, handler_class):
                """Record the requested address, then bind as usual."""
                type(self).requested = address
                super().__init__(address, handler_class)

        listener = server.create_server(
            LOOPBACK_HOST,
            EPHEMERAL_PORT,
            handler_class=_CountingHandler,
            server_class=_RecordingServer,
        )
        self.addCleanup(listener.server_close)
        bound_host, bound_port = listener.server_address[:2]
        self.assertIsInstance(listener, _RecordingServer)
        self.assertIs(listener.RequestHandlerClass, _CountingHandler)
        self.assertEqual(_RecordingServer.requested, (LOOPBACK_HOST, EPHEMERAL_PORT))

        thread = threading.Thread(
            target=server.serve_until_signalled, args=(listener,), daemon=True
        )
        thread.start()
        try:
            response = _perform_request(bound_host, bound_port, "GET", EXPECTED_PATH)
        finally:
            listener.shutdown()
            thread.join(PROCESS_EXIT_TIMEOUT_SECONDS)

        self.assertFalse(thread.is_alive())
        self.assertEqual(response.status, 200)
        self.assert_payload_conforms(response.json())
        self.assertEqual(_CountingHandler.answered, 1, "the substitute really served")

    def test_binding_port_zero_yields_a_real_assigned_port(self):
        """The kernel's choice is readable from ``server_address``, which is the point."""
        listener, bound_host, bound_port = self._bind()
        self.assertEqual(bound_host, LOOPBACK_HOST)
        self.assertGreater(bound_port, 0)
        self.assertLessEqual(bound_port, 65535)
        self.assertEqual(listener.socket.getsockname()[:2], (bound_host, bound_port))

    def test_the_listener_uses_the_contract_handler_by_default(self):
        """The default handler is the one that defines the contract."""
        listener, _, _ = self._bind()
        self.assertIs(listener.RequestHandlerClass, health.HealthRequestHandler)

    def test_binding_is_separate_from_serving(self):
        """Nothing is served until the caller says so, which is what makes this testable.

        The socket is listening -- a connection is accepted by the kernel's backlog
        -- but no response arrives, because no request has been processed yet.
        """
        _, bound_host, bound_port = self._bind()
        connection = _RawConnection(bound_host, bound_port, timeout=0.3)
        self.addCleanup(connection.close)
        connection.send(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        with self.assertRaises((TimeoutError, OSError)):
            connection.read_response()

    def test_an_ipv6_host_binds_the_ipv6_family(self):
        """The explicit family selection is what makes ``HEALTH_HOST=::1`` work."""
        if not socket.has_ipv6:
            self.skipTest("this host has no IPv6 support")
        try:
            listener, bound_host, _ = self._bind("::1", EPHEMERAL_PORT)
        except OSError as error:
            self.skipTest(f"IPv6 loopback is not bindable here: {error}")
        self.assertEqual(listener.address_family, socket.AF_INET6)
        self.assertEqual(bound_host, "::1")

    def test_an_occupied_port_raises_and_leaks_no_descriptor(self):
        """``create_server`` reports a taken port by raising, for ``main`` to translate."""
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind((LOOPBACK_HOST, EPHEMERAL_PORT))
        occupied.listen(1)
        taken_port = occupied.getsockname()[1]

        with self.assertRaises(OSError) as captured:
            server.create_server(LOOPBACK_HOST, taken_port)
        self.assertEqual(captured.exception.errno, errno.EADDRINUSE)

        reason, remedy = server._describe_bind_failure(taken_port, captured.exception)
        self.assertEqual(reason, "address already in use")
        self.assertIn(health.ENV_PORT, remedy)

    def test_serving_off_the_main_thread_answers_and_then_releases_the_port(self):
        """The whole serve-and-stop cycle, driven without a signal.

        ``serve_until_signalled`` installs signal handlers only where it can; off
        the main thread ``signal.signal`` raises, which is caught so that serving
        proceeds regardless and the caller stops the listener itself.  That path is
        exercised here, and the assertion that matters is the last one: after the
        call returns, the port is *free*, not merely quiet.
        """
        listener, bound_host, bound_port = self._bind()
        statuses = []
        thread = threading.Thread(
            target=lambda: statuses.append(server.serve_until_signalled(listener)),
            name="blitzy-serve-until-signalled",
            daemon=True,
        )
        thread.start()
        try:
            response = _perform_request(bound_host, bound_port, "GET", EXPECTED_PATH)
            self.assertEqual(response.status, 200)
            self.assert_payload_conforms(response.json())
        finally:
            listener.shutdown()
            thread.join(PROCESS_EXIT_TIMEOUT_SECONDS)

        self.assertFalse(thread.is_alive(), "serve_until_signalled must return")
        self.assertEqual(statuses, [server.EXIT_OK], "an asked-for stop is a success")
        self.assertTrue(
            _await_port_release(bound_host, bound_port),
            "the socket must be released, not merely silenced",
        )

    def test_a_released_port_can_be_rebound_immediately(self):
        """The proof that the release is real: the same port is taken again at once.

        This is the property any immediate restart depends on.
        """
        listener, bound_host, bound_port = self._bind()
        thread = threading.Thread(
            target=server.serve_until_signalled, args=(listener,), daemon=True
        )
        thread.start()
        _perform_request(bound_host, bound_port, "GET", EXPECTED_PATH)
        listener.shutdown()
        thread.join(PROCESS_EXIT_TIMEOUT_SECONDS)
        self.assertFalse(thread.is_alive())

        rebound = server.create_server(bound_host, bound_port)
        self.addCleanup(rebound.server_close)
        self.assertEqual(rebound.server_address[:2], (bound_host, bound_port))

    def test_a_real_signal_stops_serving_and_the_disposition_is_restored(self):
        """A genuine ``SIGTERM``, delivered in process, on the main thread.

        This is the one case that exercises the deadlock-avoidance design for
        real: the handler runs on the thread that is inside ``serve_forever``, and
        the parked helper thread is what actually calls ``shutdown()``.  A handler
        that called ``shutdown()`` itself would hang here rather than fail, which
        is why the join below is bounded and its result asserted.

        A sentinel handler is installed first so that a mistimed signal can never
        reach the default disposition and kill the test process; the signal is only
        sent once the endpoint has answered, which proves the real handlers are
        already in place.
        """
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers can only be installed on the main thread")

        def _sentinel(_signum, _frame):
            """Absorb a signal that arrives outside the window under test."""

        for signal_number in server.SHUTDOWN_SIGNALS:
            previous = signal.signal(signal_number, _sentinel)
            self.addCleanup(signal.signal, signal_number, previous)

        listener, bound_host, bound_port = self._bind()
        failures = []

        def _stop_once_serving():
            """Wait until the endpoint answers, then ask this process to stop."""
            deadline = time.monotonic() + PROCESS_STARTUP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                try:
                    _perform_request(bound_host, bound_port, "GET", EXPECTED_PATH)
                except OSError:
                    time.sleep(READINESS_PAUSE_SECONDS)
                    continue
                os.kill(os.getpid(), signal.SIGTERM)
                return
            failures.append("the listener never answered, so no signal was sent")

        stopper = threading.Thread(target=_stop_once_serving, daemon=True)
        stopper.start()
        status = server.serve_until_signalled(listener)
        stopper.join(PROCESS_EXIT_TIMEOUT_SECONDS)

        self.assertEqual(failures, [])
        self.assertEqual(status, server.EXIT_OK)
        self.assertIs(
            signal.getsignal(signal.SIGTERM),
            _sentinel,
            "the previous disposition must be restored, or the suite is poisoned",
        )
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)
        self.assertTrue(_await_port_release(bound_host, bound_port))

    def test_serving_is_silent(self):
        """Neither stream is written by serving: the start-up line is the only output.

        Per-request logging would put one line in the process log for every
        health poll, which is why the handler silences it -- and why this asserts
        the absence over a real socket rather than by reading the override.
        """
        listener, bound_host, bound_port = self._bind()
        stdout, stderr = io.StringIO(), io.StringIO()
        thread = threading.Thread(
            target=server.serve_until_signalled, args=(listener,), daemon=True
        )
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            thread.start()
            for target in (EXPECTED_PATH, "/unknown", f"/{EXPECTED_PATH}"):
                _perform_request(bound_host, bound_port, "GET", target)
            _perform_request(bound_host, bound_port, "POST", EXPECTED_PATH)
            listener.shutdown()
            thread.join(PROCESS_EXIT_TIMEOUT_SECONDS)

        self.assertFalse(thread.is_alive())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


class TestServerProcessLifecycle(PayloadContractAssertions, unittest.TestCase):
    """``python server.py`` as a real child process, from start-up to exit status.

    This is the only class that runs the entry point end to end as a program, and
    it is the only way to assert the three things that are properties of the
    *process* rather than of any function: the single announced start-up line, the
    exit status after a signal, and the two-line diagnostic when a bind fails.

    Safety is arranged rather than hoped for.  Every child is given
    ``HEALTH_HOST=127.0.0.1`` and a ``HEALTH_PORT`` that
    :func:`_reserve_free_port` has just reserved and released, so no child can
    bind the tier's port 8000 or publish a socket on another interface -- except
    the two cases that deliberately bind an address the kernel must refuse, which
    is the behaviour under test.  ``PYTHONDONTWRITEBYTECODE`` keeps a child from
    materializing ``__pycache__`` inside the repository.  Every wait is bounded and
    every child is terminated -- escalating to ``SIGKILL`` -- by a registered
    cleanup, so neither a hang nor a failure can leave a listener behind.
    """

    def _spawn(self, arguments=(), **environment_overrides):
        """Start a ``server.py`` child with guaranteed, bounded cleanup."""
        process = _ServerProcess(arguments, **environment_overrides)
        self.addCleanup(process.stop)
        return process

    def _spawn_serving(self):
        """Start a child on a reserved free loopback port and await its start-up line.

        A reserved concrete port rather than ``0``: a configured ``0`` is rejected,
        so passing it would fall through to the tier's own 8000 and publish a
        service on the port an operator expects to be the real application.
        """
        requested = _reserve_free_port()
        process = self._spawn(
            **{health.ENV_HOST: LOOPBACK_HOST, health.ENV_PORT: str(requested)}
        )
        announcement = process.await_startup()
        self.assertEqual(
            announcement["port"],
            requested,
            "the child must bind the port it was given, not fall back to another",
        )
        return process, announcement

    def test_the_process_announces_its_bound_address_in_one_line(self):
        """One line on standard output, in the documented form, naming the real port."""
        process, announcement = self._spawn_serving()
        self.assertEqual(announcement["host"], LOOPBACK_HOST)
        self.assertEqual(announcement["path"], EXPECTED_PATH)
        self.assertEqual(announcement["name"], EXPECTED_NAME)
        self.assertEqual(announcement["version"], EXPECTED_VERSION)
        self.assertGreater(announcement["port"], 0, "a real port, never 0")
        self.assertNotEqual(
            announcement["port"], TIER_PORT, "a test child must never bind 8000"
        )
        self.assertEqual(process.stdout_lines, [announcement["line"]])
        self.assertEqual(process.stderr_lines, [])

    def test_the_override_is_what_the_process_actually_binds(self):
        """End-to-end proof of the override: the announced host is the one requested.

        Without the override this process would bind ``0.0.0.0``, so the announced
        ``127.0.0.1`` could not have come from the configuration file or the
        literal.  This is the precedence chain observed from outside the process.
        """
        _, announcement = self._spawn_serving()
        self.assertEqual(announcement["host"], LOOPBACK_HOST)
        self.assertNotEqual(announcement["host"], EXPECTED_BIND_HOST)
        self.assertNotEqual(announcement["host"], health.FALLBACK_HOST)

    def test_the_announced_endpoint_serves_the_frozen_contract(self):
        """The address the process announced is the address that answers correctly."""
        _, announcement = self._spawn_serving()
        host, port = announcement["host"], announcement["port"]

        served = _perform_request(host, port, "GET", EXPECTED_PATH)
        self.assertEqual(served.status, 200)
        self.assertEqual(served.header("Content-Type"), EXPECTED_CONTENT_TYPE)
        self.assertEqual(served.header("Cache-Control"), EXPECTED_CACHE_CONTROL)
        payload = served.json()
        self.assert_payload_conforms(payload)
        self.assert_byte_shape(served.text())
        self.assertEqual(payload["name"], EXPECTED_NAME)
        self.assertEqual(payload["version"], EXPECTED_VERSION)
        self.assertEqual(payload["status"], EXPECTED_STATUS)

        rejected = _perform_request(host, port, "POST", EXPECTED_PATH)
        self.assertEqual(rejected.status, 405)
        self.assertEqual(rejected.header("Allow"), EXPECTED_ALLOW)

        unknown = _perform_request(host, port, "GET", "/unknown")
        self.assertEqual(unknown.status, 404)

        aliased = _perform_request(host, port, "GET", f"/{EXPECTED_PATH}")
        self.assertEqual(aliased.status, 404, "//health is not an alias in the real process")

    def test_the_timestamp_is_fresh_in_the_real_process(self):
        """Two probes against the running process differ only in the timestamp.

        The two probes are issued back to back with **no pause between them**.
        Pausing would test the wall clock rather than the endpoint; the contract
        requires two consecutive responses to differ, and the timestamp
        allocator hands out strictly increasing values precisely so that holds
        even when both probes land inside one millisecond.
        """
        _, announcement = self._spawn_serving()
        host, port = announcement["host"], announcement["port"]
        first = _perform_request(host, port, "GET", EXPECTED_PATH).json()
        second = _perform_request(host, port, "GET", EXPECTED_PATH).json()
        self.assertNotEqual(first["timestamp"], second["timestamp"])
        for member in ("name", "version", "status"):
            with self.subTest(member=member):
                self.assertEqual(first[member], second[member])

    def test_nothing_beyond_the_startup_line_is_ever_written(self):
        """After serving many requests, the log is still exactly one line.

        A health endpoint is polled continuously, so a single logged line per
        request would be the noisiest thing in the log.
        """
        process, announcement = self._spawn_serving()
        host, port = announcement["host"], announcement["port"]
        for _ in range(12):
            _perform_request(host, port, "GET", EXPECTED_PATH)
        _perform_request(host, port, "POST", EXPECTED_PATH)
        _perform_request(host, port, "GET", "/unknown")

        self.assertEqual(process.stdout_lines, [announcement["line"]])
        self.assertEqual(process.stderr_lines, [])

    def test_sigterm_stops_the_process_cleanly(self):
        """The orderly-stop path: exit 0, no traceback, and the port released."""
        self._assert_signal_stops_cleanly(signal.SIGTERM)

    def test_sigint_stops_the_process_cleanly(self):
        """The Ctrl-C path: identical outcome, by a different route through the code."""
        self._assert_signal_stops_cleanly(signal.SIGINT)

    def _assert_signal_stops_cleanly(self, signal_number):
        """Send *signal_number* to a serving child and assert an orderly exit."""
        process, announcement = self._spawn_serving()
        host, port = announcement["host"], announcement["port"]
        self.assertEqual(
            _perform_request(host, port, "GET", EXPECTED_PATH).status, 200
        )

        status = process.signal_and_wait(signal_number)

        self.assertEqual(
            status, server.EXIT_OK, f"{signal_number.name} is an asked-for stop"
        )
        self.assertEqual(process.stdout_lines, [announcement["line"]])
        self.assertEqual(process.stderr_lines, [], "an orderly stop prints nothing")
        self.assertNotIn("Traceback", "\n".join(process.stderr_lines))
        self.assertTrue(
            _await_port_release(host, port),
            "an orderly exit must release the listening socket",
        )

    def test_a_repeated_signal_still_stops_quietly_and_releases_the_port(self):
        """A stop signalled twice stays quiet, leaves no traceback and frees the port.

        An orchestrator that retries its stop must not turn a clean shutdown into a
        noisy failure.  What it must *not* be asserted to do is exit 0 unconditionally,
        and the reason is worth stating precisely, because assuming otherwise makes
        this test intermittently red for a reason that is not a defect.

        ``serve_until_signalled`` deliberately restores the signal disposition it
        found -- so that calling it in process leaves the interpreter as it was --
        and it does so just before returning.  Between that restoration and the
        interpreter's own exit there is a narrow window in which ``SIGTERM`` is once
        again the default disposition, so a *second* signal landing in that window
        terminates the process outright and is reported as ``-SIGTERM``.  Both
        outcomes are correct answers to "stop, and stop again": the caller asked for
        termination twice and got it, quietly, with the socket released.  Only a
        non-zero *exit code* -- which is what a bind failure produces -- would mean
        something went wrong.

        That window is not hypothetical: both outcomes are reachable depending on
        how far apart the two signals land, so asserting ``0`` alone would be a
        test that fails for a reason that is not a defect.  The single-signal
        cases above pin the exact ``0`` status, so nothing is lost by admitting
        both outcomes here.
        """
        process, announcement = self._spawn_serving()
        host, port = announcement["host"], announcement["port"]
        self.assertEqual(
            _perform_request(host, port, "GET", EXPECTED_PATH).status, 200
        )
        process.send_signal(signal.SIGTERM)
        process.send_signal(signal.SIGTERM)
        status = process.wait()

        self.assertIn(
            status,
            (server.EXIT_OK, -int(signal.SIGTERM)),
            "a retried stop is either an orderly exit or a termination by that signal,"
            f" never a failure status -- got {status!r}",
        )
        self.assertNotEqual(
            status,
            server.EXIT_BIND_FAILURE,
            "a retried stop must never be reported as a bind failure",
        )
        self.assertEqual(process.stdout_lines, [announcement["line"]])
        self.assertEqual(process.stderr_lines, [], "no traceback, no extra line")
        self.assertTrue(_await_port_release(host, port))

    def test_an_occupied_port_is_reported_and_the_process_exits_one(self):
        """The bind-failure path end to end: status 1 and an actionable diagnostic.

        The port is occupied by a *listening* socket in this process, because
        ``SO_REUSEADDR`` -- which the listener sets so it can rebind promptly --
        does not permit a second bind while a listener is active.
        """
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind((LOOPBACK_HOST, EPHEMERAL_PORT))
        occupied.listen(1)
        taken_port = occupied.getsockname()[1]

        process = self._spawn(
            **{health.ENV_HOST: LOOPBACK_HOST, health.ENV_PORT: str(taken_port)}
        )
        status = process.wait()

        self.assertEqual(status, server.EXIT_BIND_FAILURE)
        self.assertEqual(process.stdout_lines, [], "a failed bind announces nothing")
        self.assertEqual(len(process.stderr_lines), 2, "two lines, never a traceback")
        first, second = process.stderr_lines
        self.assertTrue(first.startswith("server.py: "))
        self.assertTrue(second.startswith("server.py: "))
        self.assertIn(f"cannot bind {LOOPBACK_HOST}:{taken_port}", first)
        self.assertIn("address already in use", first)
        self.assertIn(health.ENV_PORT, second)
        self.assertIn("python server.py", second)
        self.assertNotIn("Traceback", "\n".join(process.stderr_lines))

    def test_an_unavailable_address_is_reported_and_the_process_exits_one(self):
        """A host that is not on this machine is refused, and said so plainly.

        ``192.0.2.1`` is reserved by RFC 5737 for documentation and is never
        assigned to a local interface, so the kernel refuses the bind without any
        network traffic.
        """
        process = self._spawn(
            **{health.ENV_HOST: "192.0.2.1", health.ENV_PORT: str(_reserve_free_port())}
        )
        status = process.wait()

        self.assertEqual(status, server.EXIT_BIND_FAILURE)
        self.assertEqual(process.stdout_lines, [])
        self.assertEqual(len(process.stderr_lines), 2)
        first, second = process.stderr_lines
        self.assertIn("cannot bind 192.0.2.1:", first)
        self.assertIn("address not available on this host", first)
        self.assertIn(health.ENV_HOST, second)
        self.assertNotIn("Traceback", "\n".join(process.stderr_lines))

    def test_a_command_line_argument_is_ignored_rather_than_rejected(self):
        """Configuration is the environment, so an argument changes nothing.

        ``server.py`` parses no flags by design: the environment and
        ``config/health.json`` are the two sources of truth, and a flag would be a
        third.  An argument is therefore ignored -- the process still binds what it
        was configured to bind, still announces one line, and still serves -- rather
        than being rejected with a usage error.  Passing one for real is what makes
        this a test of behaviour instead of a restatement of the docstring.
        """
        requested = _reserve_free_port()
        process = self._spawn(
            ("--port=9999", "unexpected"),
            **{health.ENV_HOST: LOOPBACK_HOST, health.ENV_PORT: str(requested)},
        )
        announcement = process.await_startup()
        self.assertEqual(announcement["port"], requested)

        self.assertEqual(announcement["host"], LOOPBACK_HOST)
        self.assertNotEqual(
            announcement["port"], 9999, "the argument must not have been parsed"
        )
        self.assertEqual(
            _perform_request(
                announcement["host"], announcement["port"], "GET", EXPECTED_PATH
            ).status,
            200,
        )
        self.assertEqual(process.stdout_lines, [announcement["line"]])
        self.assertEqual(process.stderr_lines, [], "no usage error, no warning")


if __name__ == "__main__":
    # Direct execution mirrors what `python -m unittest` does through discovery,
    # so a contributor can run this one file without remembering an incantation.
    unittest.main(verbosity=2)
