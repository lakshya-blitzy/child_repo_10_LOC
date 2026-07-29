"""Level 2 (``child_repo_10_LOC``) health payload builder and HTTP request handler.

This module is the whole of the Python tier's ``/health`` implementation apart
from the listener itself:

* it resolves this application's identity and serving parameters **once**, at
  import time, through the uniform precedence chain
  ``environment variable -> configuration file -> compiled-in literal``;
* it builds the frozen four-member health payload, reading the clock on every
  call so the ``timestamp`` member proves liveness rather than mere
  reachability;
* it serializes that payload to the exact bytes the contract mandates; and
* it exposes :class:`HealthRequestHandler`, a
  :class:`http.server.BaseHTTPRequestHandler` subclass implementing the
  contract's routing, status codes and headers.

The module deliberately **never binds a socket**.  Binding is the sibling
``server.py``'s single responsibility, which keeps this module importable by
the test suite with no listener and no port in play, and keeps the payload
builder unit-testable without any network involvement at all.

The frozen response contract -- normative source ``docs/health-endpoint.md`` in
the apex repository, implemented identically by the JavaScript tier's
``health.js`` and the Java tier's ``HealthServer.java``::

    GET|HEAD /health      -> 200, the four-member body
    GET      /health?x=1  -> 200, identical (the query string is ignored)
    GET      /health/     -> 404 (the path comparison is exact; no normalizing)
    GET      /unknown     -> 404 {"error":"Not Found"}
    POST     <anything>   -> 405 {"error":"Method Not Allowed"} + Allow: GET, HEAD

    Content-Type:  application/json; charset=utf-8
    Cache-Control: no-store
    Body:          {"name":...,"version":...,"timestamp":...,"status":"UP"}

Two clauses of that contract are byte-level requirements rather than stylistic
preferences, and both are enforced here:

1. ``json.dumps`` defaults to ``", "``/``": "`` separators, which diverges
   byte-for-byte from the JavaScript tier's ``JSON.stringify`` (compact by
   default).  Every serialization in this module therefore passes
   ``separators=(",", ":")``.  Measured over the canonical field values:
   compact md5 ``7b23468a1b5d1c6518c47d9e87778edb`` versus default-separator
   md5 ``d32e3993d1e52c7a5caad222a75cc437`` -- different bytes for identical
   data, which is why the argument is mandatory rather than cosmetic.
2. ``datetime.isoformat()`` emits six fractional digits and a ``+00:00``
   offset, which fails the contract's timestamp pattern
   ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$``.  The timestamp is
   therefore formatted explicitly to exactly three fractional digits with a
   ``Z`` suffix, derived from a single clock read.

Standard library only.  This repository has zero third-party runtime
dependencies, and keeping it that way is a requirement rather than an
accident: the composition's security posture rests on it.

Consumers of this module: ``server.py`` (binds the listener using the resolved
host/port and this handler), ``test_app.py`` (asserts the payload contract with
no socket), the sibling ``Dockerfile`` (whose ``HEALTHCHECK`` probes the running
endpoint) and ``.github/workflows/health-check.yml`` (syntax gate plus a live
black-box probe).
"""

import json
import os
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    # Resolved configuration (read once, at import).
    "APP_NAME",
    "APP_VERSION",
    "HOST",
    "PORT",
    "HEALTH_PATH",
    "STATUS",
    "get_config",
    # Declared configuration sources, override names and literal fallbacks --
    # the three links of the precedence chain, exported so a consumer can
    # assert the chain rather than restate its values.
    "MODULE_DIR",
    "PYPROJECT_PATH",
    "CONFIG_PATH",
    "ENV_HOST",
    "ENV_PORT",
    "FALLBACK_NAME",
    "FALLBACK_VERSION",
    "FALLBACK_HOST",
    "FALLBACK_PORT",
    "FALLBACK_PATH",
    "FALLBACK_STATUS",
    # The frozen contract, expressed as constants.
    "CONTENT_TYPE",
    "CACHE_CONTROL",
    "ALLOW_HEADER_VALUE",
    "ALLOWED_METHODS",
    "PAYLOAD_KEYS",
    "JSON_SEPARATORS",
    "JSON_ENCODING",
    "STATUS_OK",
    "STATUS_NOT_FOUND",
    "STATUS_METHOD_NOT_ALLOWED",
    "NOT_FOUND_BODY",
    "METHOD_NOT_ALLOWED_BODY",
    # Payload construction.
    "current_timestamp",
    "build_payload",
    "serialize",
    "render_payload",
    # Request handling.
    "HealthRequestHandler",
    "HealthHandler",
    "HealthCheckHandler",
]

# ---------------------------------------------------------------------------
# The frozen contract, expressed as constants
#
# Nothing below is configurable, because none of it is configuration: these are
# the wire-level terms every tier implements identically.  They live here as
# named constants so that the handler never spells a contract value inline and
# so the test suite can assert against the same single source.
# ---------------------------------------------------------------------------

#: Success media type.  Deliberately ``application/json`` rather than the
#: health-check draft's ``application/health+json`` so that ordinary tooling --
#: ``curl``, ``jq``, the GitHub Actions runner -- parses the payload with no
#: special handling.  The space after the semicolon is part of the value.
CONTENT_TYPE = "application/json; charset=utf-8"

#: Sent on every response, success and error alike, so a poller always reads
#: live state rather than an intermediary's cached copy of an earlier answer.
CACHE_CONTROL = "no-store"

#: The only two methods the endpoint serves.  Every other method -- standard,
#: exotic or malformed -- receives ``405``, never ``501``.
ALLOWED_METHODS = ("GET", "HEAD")

#: Exact value of the ``Allow`` header on a ``405``: uppercase method names,
#: one comma, one space, in this order.  Derived from :data:`ALLOWED_METHODS`
#: rather than spelled again, so the header can never disagree with the set of
#: methods the handler actually serves.
ALLOW_HEADER_VALUE = ", ".join(ALLOWED_METHODS)

#: The four body members, in their frozen wire order -- the order
#: :func:`build_payload` inserts them in and the order the test suite and CI
#: assert.  Both the count and the order are part of the contract, and no fifth
#: member may ever be added: an extension point in a frozen contract is a drift
#: point.
PAYLOAD_KEYS = ("name", "version", "timestamp", "status")

#: Compact JSON separators.  See the module docstring for the measured md5
#: divergence this argument prevents.  Never ``sort_keys`` (it would reorder the
#: members to ``name, status, timestamp, version``) and never ``indent``.
JSON_SEPARATORS = (",", ":")

#: RFC 8259 requires UTF-8, and ``Content-Length`` is the length of the encoded
#: byte sequence rather than a character count.
JSON_ENCODING = "utf-8"

#: The three status codes the contract defines.  There is deliberately no 5xx:
#: the handler performs no I/O beyond reading already-loaded values and a
#: clock, so it has no failure path to report.
STATUS_OK = 200
STATUS_NOT_FOUND = 404
STATUS_METHOD_NOT_ALLOWED = 405

#: ``strftime`` pattern for everything up to (not including) the fractional
#: second.  The milliseconds and the ``Z`` suffix are appended explicitly.
_TIMESTAMP_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# ---------------------------------------------------------------------------
# Compiled-in literal fallbacks
#
# The last link in the precedence chain, and a requirement rather than a
# nicety: they guarantee the endpoint still serves a valid contract when a
# configuration file is missing from a container image -- precisely the failure
# mode a health endpoint has to survive.  An endpoint that cannot answer
# because its own configuration is absent is worse than no endpoint at all.
# ---------------------------------------------------------------------------

FALLBACK_NAME = "child_repo_10_LOC"
FALLBACK_VERSION = "1.0.0"
FALLBACK_HOST = "0.0.0.0"
FALLBACK_PORT = 8000
FALLBACK_PATH = "/health"
FALLBACK_STATUS = "UP"

#: Environment overrides for this tier.  Levels 2 and 3 prefix their variables
#: while Level 1 uses the bare ``PORT``/``HOST``; that asymmetry is deliberate
#: and must not be "harmonized".  This module never reads ``PORT`` or ``HOST``.
ENV_HOST = "HEALTH_HOST"
ENV_PORT = "HEALTH_PORT"

#: Inclusive bounds accepted for a port.  ``0`` is permitted and means "let the
#: operating system assign an ephemeral port", which is how a test can bind a
#: listener without colliding with a running server on 8000.
_MIN_PORT = 0
_MAX_PORT = 65535

# ---------------------------------------------------------------------------
# Declared configuration sources
#
# Resolved from this module's own location, never from the current working
# directory, so the values are identical whether the server is launched from
# the repository root, from a parent directory, or as ``/app/server.py`` inside
# the container image.
# ---------------------------------------------------------------------------

MODULE_DIR = Path(__file__).resolve().parent

#: Identity source: ``[project] name`` and ``[project] version``.
PYPROJECT_PATH = MODULE_DIR / "pyproject.toml"

#: Serving source: ``host``, ``port``, ``path`` and ``status``.
CONFIG_PATH = MODULE_DIR / "config" / "health.json"


def _coerce_text(value, fallback):
    """Return ``value`` as a non-empty stripped string, else ``fallback``.

    Configuration arrives from a TOML document, a JSON document or the process
    environment, so a value can legitimately be of any type or be blank.  Only
    a genuinely usable string is accepted; anything else falls through to the
    next link in the precedence chain.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return fallback


def _coerce_port(value, fallback):
    """Return ``value`` as an in-range TCP port number, else ``fallback``.

    Accepts an ``int`` (as JSON supplies) or a decimal string (as the
    environment supplies).  ``bool`` is rejected explicitly because it is an
    ``int`` subclass and ``HEALTH_PORT=True`` is a configuration error, not
    port 1.  A non-numeric or out-of-range value falls through rather than
    raising, so a typo in the environment can never stop the endpoint serving.
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = int(value.strip(), 10)
        except ValueError:
            return fallback
    else:
        return fallback
    if _MIN_PORT <= candidate <= _MAX_PORT:
        return candidate
    return fallback


def _read_identity(path):
    """Read ``(name, version)`` from a ``pyproject.toml``.  Never raises.

    ``tomllib.load`` requires a binary stream, hence ``"rb"``.  Every failure
    mode collapses to the literal fallbacks: a missing or unreadable file
    (``OSError``), malformed TOML (``tomllib.TOMLDecodeError``, a ``ValueError``
    subclass), an absent ``[project]`` table or key (``KeyError``), or a
    document whose ``project`` entry is not a table (``TypeError``).
    """
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        name = project["name"]
        version = project["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return FALLBACK_NAME, FALLBACK_VERSION
    return (
        _coerce_text(name, FALLBACK_NAME),
        _coerce_text(version, FALLBACK_VERSION),
    )


def _read_serving_document(path):
    """Read ``config/health.json`` into a mapping.  Never raises.

    Returns an empty mapping on any failure -- missing or unreadable file
    (``OSError``), malformed JSON (``json.JSONDecodeError``, a ``ValueError``
    subclass), undecodable bytes (``UnicodeDecodeError``, likewise a
    ``ValueError``), or a document whose top level is not an object -- so that
    every individual value then resolves to its literal fallback.
    """
    try:
        with open(path, "rb") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if isinstance(document, dict):
        return document
    return {}


def _resolve_host(document):
    """Resolve the bind address: ``HEALTH_HOST`` -> config -> literal."""
    override = _coerce_text(os.environ.get(ENV_HOST), None)
    if override is not None:
        return override
    return _coerce_text(document.get("host"), FALLBACK_HOST)


def _resolve_port(document):
    """Resolve the bind port: ``HEALTH_PORT`` -> config -> literal.

    An override that is set but unusable (``HEALTH_PORT=abc``, ``=99999``) does
    not short-circuit to the literal; it falls through to the configuration
    file first, so the chain degrades one link at a time.
    """
    override = _coerce_port(os.environ.get(ENV_PORT), None)
    if override is not None:
        return override
    return _coerce_port(document.get("port"), FALLBACK_PORT)


def _resolve_path(document):
    """Resolve the served path: config -> literal.

    There is deliberately no environment override.  The contract's override
    table defines exactly two variables for this tier, and the resource path is
    part of the contract rather than a deployment parameter.  A configured
    value that is not absolute is rejected, because a relative path could never
    match a parsed request target.
    """
    candidate = _coerce_text(document.get("path"), FALLBACK_PATH)
    if not candidate.startswith("/"):
        return FALLBACK_PATH
    return candidate


def _resolve_status(document):
    """Resolve the reported status literal: config -> literal (``UP``)."""
    return _coerce_text(document.get("status"), FALLBACK_STATUS)


# ---------------------------------------------------------------------------
# One-time resolution
#
# Executed exactly once, when this module is first imported.  The request
# handler never re-reads a file: a health probe must stay lightweight, and a
# probe that performs per-request I/O becomes a source of the very load it
# exists to report on.  Only the clock is read per request.
# ---------------------------------------------------------------------------

_SERVING_DOCUMENT = _read_serving_document(CONFIG_PATH)

#: Application identity, from ``pyproject.toml`` with literal fallbacks.  There
#: is no environment override for either value: identity is declared by the
#: repository, not by the deployment.
APP_NAME, APP_VERSION = _read_identity(PYPROJECT_PATH)

#: Resolved serving parameters.  ``server.py`` consumes these rather than
#: duplicating the defaults, so there is exactly one place in this tier where
#: the precedence chain is applied.
HOST = _resolve_host(_SERVING_DOCUMENT)
PORT = _resolve_port(_SERVING_DOCUMENT)
HEALTH_PATH = _resolve_path(_SERVING_DOCUMENT)
STATUS = _resolve_status(_SERVING_DOCUMENT)


def get_config():
    """Return the resolved configuration as a fresh, independent ``dict``.

    A copy is returned on every call so that a caller -- ``server.py`` when it
    logs the bound address, or the test suite when it asserts the resolution
    chain -- cannot mutate the module's resolved state.

    >>> config = get_config()
    >>> sorted(config)
    ['host', 'name', 'path', 'port', 'status', 'version']
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "host": HOST,
        "port": PORT,
        "path": HEALTH_PATH,
        "status": STATUS,
    }


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def current_timestamp():
    """Return the current UTC time as RFC 3339 with millisecond precision.

    The mandated form is ``YYYY-MM-DDTHH:MM:SS.mmmZ``: a ``Z`` suffix rather
    than a numeric offset, and exactly three fractional digits that are never
    dropped even when they are zero.

    ``datetime.isoformat()`` cannot be used: it emits six fractional digits and
    ``+00:00``.  The clock is read **once** per call and both halves of the
    result are derived from that single value, so the seconds and the
    milliseconds can never come from different instants.
    """
    moment = datetime.now(timezone.utc)
    milliseconds = moment.microsecond // 1000
    return f"{moment.strftime(_TIMESTAMP_SECONDS_FORMAT)}.{milliseconds:03d}Z"


def build_payload():
    """Build the frozen four-member health payload.

    Socket-free and side-effect-free: callable directly by the test suite with
    no listener bound.  ``name``, ``version`` and ``status`` come from the
    configuration resolved at import; only ``timestamp`` is evaluated here, on
    every call.

    The members are inserted in the contract's frozen order -- ``name``,
    ``version``, ``timestamp``, ``status`` (see :data:`PAYLOAD_KEYS`).  Python
    dictionaries preserve insertion order and ``json.dumps`` honours it, so the
    insertion order below *is* the wire order.

    A fresh timestamp on every call is what makes the endpoint proof of
    liveness rather than proof of reachability: a process that had frozen after
    binding its socket could otherwise keep serving a stale but well-formed
    payload and a poller would call it healthy.  The value is therefore never
    cached, never memoized and never captured at start-up.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "timestamp": current_timestamp(),
        "status": STATUS,
    }


def serialize(document):
    """Serialize ``document`` to the contract's exact bytes.

    Compact separators and UTF-8, with no insignificant whitespace, so that
    this tier's output is byte-shape-identical to the JavaScript and Java
    tiers' output for the same field values.
    """
    return json.dumps(document, separators=JSON_SEPARATORS).encode(JSON_ENCODING)


def render_payload():
    """Return a freshly built health payload as contract-conformant bytes."""
    return serialize(build_payload())


#: Pre-serialized error bodies.  Both are constant, so they are built once at
#: import rather than on every rejected request, and both use the same compact
#: separators as the success body so all three tiers agree byte-for-byte.
#: Measured lengths: 21 bytes and 30 bytes respectively.
NOT_FOUND_BODY = serialize({"error": "Not Found"})
METHOD_NOT_ALLOWED_BODY = serialize({"error": "Method Not Allowed"})


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


class HealthRequestHandler(BaseHTTPRequestHandler):
    """``BaseHTTPRequestHandler`` implementing the frozen health contract.

    Routing, in this order:

    1. **Method first.**  Anything other than ``GET`` or ``HEAD`` is answered
       ``405`` with ``Allow: GET, HEAD``, whatever the path -- so ``POST
       /unknown`` is a ``405`` and not a ``404``.  A caller therefore always
       learns the most actionable fact first: that its method is not permitted
       anywhere on this server.
    2. **Then path, compared exactly.**  The *parsed* path of the request
       target is compared for equality against the resolved
       :data:`HEALTH_PATH`.  A query string is ignored entirely, so
       ``/health?x=1`` succeeds; trailing slashes are not normalized, so
       ``/health/`` is a ``404``.

    Every response -- ``200``, ``404`` and ``405`` alike -- carries
    ``Content-Type: application/json; charset=utf-8``, ``Cache-Control:
    no-store`` and an explicit ``Content-Length``.  Only the ``405`` adds
    ``Allow``.

    The handler performs no I/O beyond reading the already-resolved
    configuration and the clock: no file access, no network call and no
    dependency interrogation.  It follows that the handler has no failure path
    of its own, which is why the contract defines no ``5xx`` response and why
    none is synthesized here.

    Bind this class with the listener of your choice; ``server.py`` uses
    :class:`http.server.ThreadingHTTPServer`.  This module never binds a socket
    itself.

    Testing hooks, deliberately provided so the class can be exercised without
    monkeypatching module state: :attr:`health_path` is read through the class,
    so a subclass can answer on a different path, and the payload is rendered
    through the module-level :func:`render_payload`, so a test may substitute
    that function.
    """

    #: HTTP/1.1 is safe here -- and is the version the contract's documented
    #: examples show -- precisely because every response sets an accurate
    #: ``Content-Length``, so a persistent connection is always correctly
    #: framed.
    protocol_version = "HTTP/1.1"

    #: Keep the ``Server`` header free of interpreter version information: a
    #: liveness probe has no need to advertise the runtime it is built on.
    #: ``sys_version`` is emptied and :meth:`version_string` is overridden --
    #: the first suppresses the version, the second suppresses the separator
    #: space the base implementation would otherwise leave behind, so the header
    #: reads exactly ``Server: health``.
    server_version = "health"
    sys_version = ""

    #: Idle timeout, in seconds, applied to each accepted connection.  Without
    #: it a client that opens a persistent connection and never completes a
    #: request would hold a worker thread for the lifetime of the process.  A
    #: probe completes in single-digit milliseconds, so this is generous.
    timeout = 10

    #: The resolved path this handler answers on.  Read through the class so a
    #: subclass can be pointed elsewhere without mutating module state.
    health_path = HEALTH_PATH

    #: Upper bound on a request body this handler will consume before giving up
    #: and closing the connection.  Nothing legitimate sends a body to this
    #: endpoint; the drain exists only to keep a persistent connection framed.
    _max_drain_bytes = 1 << 20
    _drain_chunk_bytes = 1 << 16

    #: Protocol-level errors are raised by the base class *before* routing ever
    #: happens -- a malformed request line (``400``), an over-long request
    #: target (``414``), too many header fields (``431``).  They are outside the
    #: contract, which describes routed responses, but the base class would
    #: answer them with a roughly half-kilobyte HTML page.  These two supported
    #: hooks give those bodies the same compact single-member JSON shape as the
    #: contract's own error bodies, so every byte this server ever emits is
    #: JSON.  See :meth:`send_error` for why the format interpolates only the
    #: reason phrase.
    error_content_type = CONTENT_TYPE
    error_message_format = '{"error":"%(message)s"}'

    # -- Method dispatch ---------------------------------------------------

    def do_GET(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        """Serve ``GET``: the payload on the health path, ``404`` elsewhere."""
        self._serve(write_body=True)

    def do_HEAD(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        """Serve ``HEAD``: identical status and headers to ``GET``, no body.

        The payload is still built and measured so that ``Content-Length``
        equals the byte length a ``GET`` would have produced.  A ``HEAD`` is
        therefore a valid, cheap liveness check that still proves the payload
        could be built.
        """
        self._serve(write_body=False)

    def do_POST(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        """Reject ``POST`` with ``405``.

        Spelled out explicitly because ``POST /health`` is the negative case
        the pipeline asserts; every other disallowed method reaches the same
        responder through :meth:`__getattr__`.
        """
        self._respond_method_not_allowed()

    def __getattr__(self, name):
        """Route any other ``do_<METHOD>`` lookup to the ``405`` responder.

        ``BaseHTTPRequestHandler`` dispatches by attribute name and answers
        ``501 Unsupported method`` when no matching ``do_*`` exists.  ``501`` is
        not the contract, and enumerating verbs would still leave exotic ones
        (``TRACE``, ``PROPFIND``, or an outright invalid token) answering
        ``501``.  Intercepting the lookup covers every method uniformly with a
        single code path.

        Only ``do_``-prefixed lookups are intercepted.  Anything else raises
        ``AttributeError`` exactly as it normally would, so ``hasattr`` checks
        elsewhere in the standard library keep their usual meaning.  Note that
        ``__getattr__`` is consulted only after normal lookup fails, so the
        ``do_GET``/``do_HEAD``/``do_POST`` methods defined above always win.
        """
        if name.startswith("do_"):
            return self._respond_method_not_allowed
        raise AttributeError(name)

    # -- Responders --------------------------------------------------------

    def _serve(self, write_body):
        """Answer an allowed method: ``200`` on the health path, else ``404``."""
        try:
            if self._requested_path() == self.health_path:
                self._send(STATUS_OK, render_payload(), write_body=write_body)
            else:
                self._send(
                    STATUS_NOT_FOUND, NOT_FOUND_BODY, write_body=write_body
                )
        except Exception:  # noqa: BLE001 - see _abandon_connection
            self._abandon_connection()

    def _respond_method_not_allowed(self):
        """Answer ``405`` with ``Allow: GET, HEAD`` and the error body."""
        try:
            drained = self._drain_request_body()
            self._send(
                STATUS_METHOD_NOT_ALLOWED,
                METHOD_NOT_ALLOWED_BODY,
                write_body=True,
                allow=ALLOW_HEADER_VALUE,
            )
            if not drained:
                # An unread request body would desynchronize a persistent
                # connection, so this one ends after the response.
                self.close_connection = True
        except Exception:  # noqa: BLE001 - see _abandon_connection
            self._abandon_connection()

    def _send(self, status, body, write_body=True, allow=None):
        """Write one complete response: status line, contract headers, body.

        ``send_response`` also emits ``Server`` and ``Date``; both are runtime
        headers outside the contract.  ``OSError`` (and its
        ``BrokenPipeError``/``ConnectionResetError``/``TimeoutError`` subclasses)
        means the peer went away mid-response, which is the client's business
        and not a fault of this application, so the connection is abandoned
        quietly rather than reported.
        """
        try:
            self.send_response(status)
            if allow is not None:
                self.send_header("Allow", allow)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Cache-Control", CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if write_body:
                self.wfile.write(body)
        except OSError:
            self._abandon_connection()

    def _abandon_connection(self):
        """Fail closed: end this connection without raising and without a 5xx.

        Reached only if something genuinely unexpected happens while answering
        a request.  Two things must not happen in that situation, and this
        method is how both are avoided.  The exception must not propagate,
        because ``socketserver`` would print a traceback and tear the
        connection down abruptly.  And no ``5xx`` may be synthesized: the
        contract defines none, the handler does no work that could fail, and by
        the time a write fails the status line has already gone out, so a
        second response could not be sent in any case.  Marking the connection
        closed is therefore the whole of the correct action -- the socket is
        released and the next probe gets a clean connection.
        """
        self.close_connection = True

    # -- Helpers -----------------------------------------------------------

    def _raw_request_target(self):
        """Recover the request target exactly as the client transmitted it.

        :attr:`~http.server.BaseHTTPRequestHandler.requestline` is captured
        while the request line is first read, *before*
        :meth:`~http.server.BaseHTTPRequestHandler.parse_request` rewrites
        :attr:`path`, so the client's original bytes survive there even when
        :attr:`path` has been altered.  A request line is
        ``METHOD SP TARGET SP VERSION`` (HTTP/1.x) or ``METHOD SP TARGET``
        (HTTP/0.9); the target is the second whitespace-delimited field.

        Returns the raw target, or ``None`` when no target can be recovered --
        in which case the caller falls back to :attr:`path`.
        """
        requestline = getattr(self, "requestline", None)
        if not isinstance(requestline, str):
            return None
        fields = requestline.split()
        if len(fields) < 2:
            return None
        return fields[1]

    def _requested_path(self):
        """Return the path this request asks for, or ``None`` if none can match.

        ``urlsplit`` discards the query string and any fragment, and copes with
        the absolute-form target (``GET http://host/health HTTP/1.1``) a proxy
        may send.  The raw :attr:`path` is never compared verbatim, because
        ``/health?x=1`` must match while ``/health/`` must not.

        One correction is applied before that comparison.  CPython's
        :meth:`~http.server.BaseHTTPRequestHandler.parse_request` collapses a
        run of leading slashes in the target to a single slash (gh-87389), so a
        client asking for ``//health`` -- or ``///health`` -- is presented to
        application code as ``/health`` and would be served the payload.  That
        rewrite exists to stop a handler which *redirects* from emitting a
        protocol-relative ``Location`` that a client would read as an absolute
        URI.  This handler never redirects; it has no ``3xx`` branch at all, so
        the rewrite protects against nothing here while silently creating extra
        undocumented aliases for the one resource this module serves.  The
        contract admits exactly one route, matched by exact comparison, so a
        target the client wrote with a doubled leading slash is reported as
        unmatchable and answered ``404`` -- strictly more restrictive than the
        standard-library behaviour, and therefore unable to reintroduce the
        open-redirect class that behaviour guards.
        """
        raw_target = self._raw_request_target()
        if raw_target is not None and raw_target.startswith("//"):
            return None
        return urlsplit(self.path).path

    def _drain_request_body(self):
        """Consume a rejected request's body so the connection stays framed.

        Returns ``True`` when there was nothing to read or the body was read in
        full, and ``False`` when the caller should close the connection
        instead: a chunked or otherwise transfer-encoded body cannot be
        measured up front, an unparseable or oversized ``Content-Length``
        cannot be trusted, and a truncated read means the peer stopped early.
        """
        headers = self.headers
        if headers is None:
            return True
        if _coerce_text(headers.get("Transfer-Encoding"), None) is not None:
            return False
        raw_length = headers.get("Content-Length")
        if raw_length is None:
            return True
        try:
            remaining = int(str(raw_length).strip(), 10)
        except ValueError:
            return False
        if remaining <= 0:
            return True
        if remaining > self._max_drain_bytes:
            return False
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, self._drain_chunk_bytes))
            if not chunk:
                return False
            remaining -= len(chunk)
        return True

    def send_error(self, code, message=None, explain=None):
        """Answer a protocol-level error with a fixed, safe reason phrase.

        The base class builds its detail message out of the offending request
        (``Bad request version ('HTTP/1."1')``, for instance).  Interpolating
        that into a JSON body is unsafe on two counts: the detail is only
        HTML-escaped, so an embedded double quote would produce invalid JSON,
        and reflecting request bytes back to an unauthenticated caller is
        needless information disclosure.

        Dropping ``message`` and ``explain`` makes the base class fall back to
        the reason phrase from its own static status table -- ``Bad Request``,
        ``URI Too Long`` -- which is never caller-controlled and contains no
        character that JSON would need escaped.  The result is always a valid,
        compact ``{"error":"<reason phrase>"}`` body.

        This is not a ``5xx`` branch and not a routed response: these codes come
        from the base class's request parsing, which runs before this handler
        sees a method or a path.  The contract's own responses never pass
        through here.
        """
        super().send_error(code)

    def version_string(self):
        """Return the ``Server`` header value: the product token, nothing else.

        The base implementation concatenates ``server_version`` and
        ``sys_version`` around a space, which with an emptied ``sys_version``
        would emit a trailing space.  Header values are trimmed by every
        conformant parser, but emitting one at all is sloppy, so the token is
        returned on its own.
        """
        return self.server_version

    # -- Logging -----------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Silence per-request logging.

        The base implementation writes a line to standard error for every
        request, which would pollute CI output and the container log with one
        entry per health poll -- and the contract forbids logging inside the
        handler at all.  ``log_request`` and ``log_error`` both funnel through
        this method, so overriding it alone silences every per-request line,
        including the timeout notice for an idle connection.  The signature
        matches the standard library exactly, shadowed builtin name included,
        so the override is a drop-in replacement.  Start-up logging belongs to
        the entry point, which announces its bound address once.
        """


#: Documented aliases.  The class above is the canonical name; these exist so
#: that a consumer importing under either common spelling resolves the same
#: object rather than failing at import time.
HealthHandler = HealthRequestHandler
HealthCheckHandler = HealthRequestHandler
