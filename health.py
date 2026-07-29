"""Health payload builder and HTTP request handler for ``child_repo_10_LOC``.

What this module does:

* it resolves this application's identity and serving parameters -- and only
  those -- **once**, at import time, through distinct precedence chains.  They
  are deliberately not uniform, because not every setting is a deployment
  parameter:

  =====================  ==============================================
  ``name``, ``version``  ``pyproject.toml`` -> compiled-in literal
  ``host``, ``port``     ``HEALTH_HOST``/``HEALTH_PORT`` ->
                         ``config/health.json`` -> compiled-in literal
  ``path``, ``status``   ``config/health.json`` -> compiled-in literal,
                         with the declaration *validated* against the
                         contract before it is adopted
  =====================  ==============================================

  Identity is declared by the repository rather than by whoever starts the
  process, and the contract admits exactly one legal value for the path and the
  status, so neither group takes an environment override.  Only the bind address
  is a free deployment concern, and only it has one.  :func:`get_config_sources`
  reports which link supplied each value, so an adopted declaration and a
  refused one are distinguishable even though both yield the literal;
* it records *why* a declared source was not used, so that falling back to a
  literal is survivable **and** observable -- see
  :func:`describe_configuration_degradation`, which ``server.py`` reports once
  at start-up;
* it builds the frozen four-member health payload, reading the clock on every
  call and handing out a *strictly increasing* millisecond value, so two
  consecutive responses always differ and ``timestamp`` proves liveness rather
  than mere reachability;
* it serializes that payload to the exact bytes the contract mandates; and
* it exposes :class:`HealthRequestHandler`, a
  :class:`http.server.BaseHTTPRequestHandler` subclass implementing the
  contract's routing, status codes and headers.

The module deliberately **never binds a socket**.  Binding is the sibling
``server.py``'s single responsibility, which keeps this module importable by
the test suite with no listener and no port in play, and keeps the payload
builder unit-testable without any network involvement at all.

Importing it is likewise **byte-silent on both streams**, even when its
configuration is missing or broken.  A module that writes when it is merely read
corrupts the output of every program that reads it, so diagnostics are *recorded*
here and *reported* by the entry point.  The single exception is
:func:`_report_handler_failure`, which writes at most one line per distinct
internal-defect category and can only fire while a request is being served --
never at import.

The frozen response contract -- normative source ``docs/health-endpoint.md`` in
the apex repository, implemented identically by the JavaScript tier's
``health.js`` and the Java tier's ``HealthServer.java``::

    GET|HEAD /health      -> 200, the four-member body
    GET      /health?x=1  -> 200, identical (the query string is ignored)
    GET      /health/     -> 404 (the path comparison is exact; no normalizing)
    GET      /%68ealth    -> 404 (the target is never percent-decoded)
    GET      ///health    -> 404 (a run of slashes is never collapsed)
    GET      /./health    -> 404 (a dot segment is never resolved)
    GET      /unknown     -> 404 {"error":"Not Found"}
    POST     <any path>   -> 405 {"error":"Method Not Allowed"} + Allow: GET, HEAD

Two values in that contract are **validated settings rather than free ones**:
the resource path ``/health`` and the ``status`` literal ``UP``.  Both are read
from ``config/health.json`` like every other setting -- it declares them so that
an operator can see the whole shape of what is served in one place -- but a
declared value is adopted only when it restates the contract's literal exactly.
Anything else is *rejected and reported* (see :func:`_resolve_frozen_value` and
:func:`frozen_value_conflicts`), because a freely settable path would move the
endpoint away from where a probe looks for it and a freely settable status would
let a deployment report a value its own behaviour does not support -- in both
cases while still returning a syntactically valid response.

The path comparison is made against the **raw origin-form request target** with
only the query component removed.  Nothing else is done to it: no dot segment is
resolved, no percent-encoded byte is decoded, no fragment is trimmed and no
absolute-form target is accepted, so ``/health`` is the one and only spelling
that answers ``200``.

    Content-Type:  application/json; charset=utf-8
    Cache-Control: no-store
    Body:          {"name":...,"version":...,"timestamp":...,"status":"UP"}

Two clauses of that contract are byte-level requirements, both measured rather
than assumed, and both are the reason for an argument that looks cosmetic:

1. ``json.dumps`` defaults to ``", "``/``": "`` separators, which diverges
   byte-for-byte from the sibling applications' compact output.  Every
   serialization here therefore passes ``separators=(",", ":")``.  Over the
   canonical field values: compact md5 ``7b23468a1b5d1c6518c47d9e87778edb``
   versus default-separator md5 ``d32e3993d1e52c7a5caad222a75cc437``.
2. ``datetime.isoformat()`` emits six fractional digits and a ``+00:00``
   offset, failing the contract's timestamp pattern
   ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$``.  The timestamp is
   formatted explicitly to three fractional digits with a ``Z`` suffix, from a
   single clock read.

Standard library only: this composition has zero third-party runtime
dependencies and its security posture rests on keeping it that way.
"""

import json
import os
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

__all__ = [
    # Resolved configuration (read once, at import).
    "APP_NAME",
    "APP_VERSION",
    "HOST",
    "PORT",
    "HEALTH_PATH",
    "HEALTH_STATUS",
    "get_config",
    # Per-value provenance, so an adopted declaration and a refused one can be
    # told apart even when both yield the same value.
    "CONFIG_SOURCES",
    "get_config_sources",
    "FROM_ENVIRONMENT",
    "FROM_FILE",
    "FROM_FALLBACK",
    # The audit trail for the two values configuration may declare but not
    # redefine.
    "FROZEN_CONFLICTS",
    "frozen_value_conflicts",
    "declared_status",
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
    # Degraded-configuration reporting: the fixed vocabulary, the recorded
    # reasons, and the one-line rendering the entry point emits.
    "SOURCE_IDENTITY",
    "SOURCE_SERVING",
    "DEGRADED_MISSING",
    "DEGRADED_UNREADABLE",
    "DEGRADED_MALFORMED",
    "DEGRADED_INCOMPLETE",
    "CONFIG_DEGRADATIONS",
    "describe_configuration_degradation",
    # Handler failure reporting: expected disconnects stay quiet, internal
    # defects are signalled once per category.
    "HANDLER_FAILURE_PREFIX",
    "HANDLER_FAILURE_SUFFIX",
    "MAX_REPORTED_HANDLER_FAILURES",
    "reported_handler_failures",
    # The frozen contract, expressed as constants.
    "CONTENT_TYPE",
    "CACHE_CONTROL",
    "ALLOW_HEADER_VALUE",
    "ALLOWED_METHODS",
    "PAYLOAD_KEYS",
    "STATUS_UP",
    "JSON_SEPARATORS",
    "JSON_ENCODING",
    "STATUS_OK",
    "STATUS_NOT_FOUND",
    "STATUS_METHOD_NOT_ALLOWED",
    "NOT_FOUND_BODY",
    "METHOD_NOT_ALLOWED_BODY",
    # Payload construction.
    "format_timestamp",
    "current_timestamp",
    "build_payload",
    "serialize",
    "render_payload",
    # Request-target routing, exposed so the rule is directly assertable.
    "request_target_path",
    # Request handling.
    "HealthRequestHandler",
    "HealthHandler",
    "HealthCheckHandler",
]

# The frozen contract, expressed as constants.  None of it is configuration:
# these are wire-level terms every tier implements identically, named here so
# the handler never spells one inline and the test suite asserts one source.

#: Success media type.  Deliberately ``application/json`` rather than the
#: health-check draft's ``application/health+json`` so that ordinary tooling --
#: ``curl``, ``jq``, any HTTP client -- parses the payload with no special
#: handling.  The space after the semicolon is part of the value.
CONTENT_TYPE = "application/json; charset=utf-8"

#: Sent on every response, success and error alike, so a poller always reads
#: live state rather than an intermediary's cached copy of an earlier answer.
CACHE_CONTROL = "no-store"

#: The only two methods served.  Every other method -- standard, exotic or
#: malformed -- receives ``405``, never ``501``.
ALLOWED_METHODS = ("GET", "HEAD")

#: Derived rather than spelled again, so the ``Allow`` header on a ``405`` can
#: never disagree with the set of methods the handler actually serves.
ALLOW_HEADER_VALUE = ", ".join(ALLOWED_METHODS)

#: The four body members, in their frozen wire order -- the order
#: :func:`build_payload` inserts them in and the order the test suite asserts.
#: Both the count and the order are part of the contract, and no fifth member
#: may ever be added: an extension point in a frozen contract is a drift point.
PAYLOAD_KEYS = ("name", "version", "timestamp", "status")

#: The one value the ``status`` member may ever carry: the literal ``UP``,
#: uppercase, exactly two characters.
#:
#: ``status`` is a **validated setting**: ``config/health.json`` is its declared
#: source and the declaration really is read, but it is adopted only when it is
#: exactly this literal.  Anything else is rejected in favour of this constant,
#: recorded by :func:`frozen_value_conflicts` and reported once at start-up.
#:
#: Both halves of that are deliberate.  The value is read from a declared source
#: because every value this endpoint serves should trace to one, rather than
#: being written inline at its point of use; and it is validated because
#: ``status`` reports what the process observed about itself while answering the
#: request, not what somebody typed.  Were the declaration honoured unchecked, a
#: deployment could publish ``"DOWN"`` from a perfectly healthy process -- or two
#: tiers of one composition could disagree about the vocabulary itself -- and
#: every consumer of the contract would be reading a claim rather than a
#: measurement.
#:
#: The Java tier applies the same rule to its own declared source.
STATUS_UP = "UP"

#: Compact JSON separators.  See the module docstring for the measured md5
#: divergence this argument prevents.  Never ``sort_keys`` (it would reorder the
#: members to ``name, status, timestamp, version``) and never ``indent``.
JSON_SEPARATORS = (",", ":")

#: ``Content-Length`` is the length of the encoded bytes, not a character count.
JSON_ENCODING = "utf-8"

#: The three status codes the contract defines, and the only three this module
#: ever sends.  No 5xx appears among them, because a routed request performs no
#: I/O -- it reads already-resolved values and one clock -- and so has no failure
#: path of its own; ``docs/health-endpoint.md`` §5.1 has no row that produces one.
#: Should something inside this process break anyway, no status outside these
#: three is invented: the fault is reported server-side and the exchange is
#: completed at the transport level, which is what
#: :meth:`HealthRequestHandler._abandon_connection` does here and what both
#: sibling tiers do too (§9.3.1).
STATUS_OK = 200
STATUS_NOT_FOUND = 404
STATUS_METHOD_NOT_ALLOWED = 405

#: ``strftime`` pattern for everything up to (not including) the fractional
#: second.  The milliseconds and the ``Z`` suffix are appended explicitly.
_TIMESTAMP_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: Divisors used to reduce a nanosecond clock reading to whole milliseconds and
#: to split those milliseconds into seconds plus a remainder.  Integer division
#: throughout: a float intermediate could round a value onto the wrong
#: millisecond, and the timestamp's whole purpose is to be exact.
_NANOSECONDS_PER_MILLISECOND = 1_000_000
_MILLISECONDS_PER_SECOND = 1_000

#: Guards :data:`_last_issued_millis` against interleaved read-then-write from
#: two handler threads.  ``server.py`` serves on ``ThreadingHTTPServer``, so
#: concurrent requests are the normal case rather than an edge case.
_timestamp_lock = threading.Lock()

#: The last millisecond value :func:`current_timestamp` handed out, or a floor
#: that any real clock reading exceeds.  See :func:`_next_timestamp_millis` for
#: what the value guarantees and why the guarantee is part of the contract.
_last_issued_millis = -1

# ---------------------------------------------------------------------------
# Compiled-in literals
#
# These are the last link in every precedence chain, and a requirement rather
# than a nicety: they guarantee the endpoint still serves a valid contract when
# a configuration file cannot be read at all -- precisely the failure mode a
# health endpoint has to survive.  An endpoint that cannot answer because its
# own configuration is absent is worse than no endpoint at all.
#
# ``FALLBACK_PATH`` and :data:`STATUS_UP` differ from the other four in one
# respect only: their chain has no environment layer and its file layer is
# *validated*, so a declaration is adopted only when it restates the literal
# exactly (see :func:`_resolve_frozen_value`).  They are still resolved values
# read from a declared source, not values written inline at their point of use.
# ---------------------------------------------------------------------------

FALLBACK_NAME = "child_repo_10_LOC"
FALLBACK_VERSION = "1.0.0"
FALLBACK_HOST = "0.0.0.0"
FALLBACK_PORT = 8000
FALLBACK_PATH = "/health"

#: The two configuration keys whose declared value may only ever restate the
#: frozen constant beside it.  Consulted by :func:`_resolve_frozen_value`, which
#: decides what is served, and by :func:`frozen_value_conflicts`, which records
#: what was refused.
_FROZEN_BY_KEY = {"path": FALLBACK_PATH, "status": STATUS_UP}

# ---------------------------------------------------------------------------
# Provenance labels
#
# Which link of a chain supplied each resolved value.  Exported through
# :func:`get_config_sources` so that an operator -- and the test suite -- can
# tell an adopted declaration from a refused one *even when the two produce the
# same value*, which is exactly the case for the two validated settings whose
# only legal declaration is the literal itself.  Without this, "the endpoint
# reports UP" is satisfied equally by a live chain and by a dead one.
# ---------------------------------------------------------------------------

FROM_ENVIRONMENT = "environment"
FROM_FILE = "file"
FROM_FALLBACK = "fallback"

#: Environment overrides for this tier.  Levels 2 and 3 prefix their variables
#: while Level 1 uses the bare ``PORT``/``HOST``; that asymmetry is deliberate
#: and must not be "harmonized".  This module never reads ``PORT`` or ``HOST``.
ENV_HOST = "HEALTH_HOST"
ENV_PORT = "HEALTH_PORT"

#: Inclusive bounds accepted for a *configured* port.  ``0`` is deliberately
#: excluded, and that exclusion is the one cross-tier port policy: all three
#: tiers reject a configured ``0`` identically.  Port ``0`` asks the operating
#: system to choose a port at random, so a process that honoured it would bind an
#: address no fixed-number probe could predict -- the endpoint would be running
#: and unreachable at the same time, which is the exact condition a health
#: endpoint exists to rule out.  A test that wants an ephemeral port still gets
#: one: it passes ``0`` to the server constructor, which is a bind-time argument
#: rather than configuration.
_MIN_PORT = 1
_MAX_PORT = 65535

# ---------------------------------------------------------------------------
# Degraded-configuration vocabulary
#
# Falling back to a literal is the right behaviour -- an endpoint that refuses
# to answer because its own configuration is absent is worse than no endpoint
# at all -- but falling back *silently* is not.  A process whose configuration
# file never arrived answers ``200`` with a complete, valid, apparently healthy
# body, so nothing at the endpoint distinguishes declared identity from
# fallback identity.  That is the failure this vocabulary exists to make
# visible: the loaders record *why* a source was not used, and the entry point
# reports it once at start-up.
#
# Every string below is a fixed literal defined in this module.  A rendered
# diagnostic is assembled only from these constants, never from a path, a
# file's contents, an environment value or an exception message, so it is
# structurally incapable of disclosing configuration data or a credential --
# and, containing no caller-controlled text, it cannot carry a control
# character into a log either.
# ---------------------------------------------------------------------------

#: Category naming the configuration source that degraded, never its path.
SOURCE_IDENTITY = "identity"
SOURCE_SERVING = "serving"

#: The file is not there -- ordinary in any deployment that did not ship it.
DEGRADED_MISSING = "declared source missing"

#: The file is there but could not be opened or read -- permissions, or a
#: directory where a file was expected.
DEGRADED_UNREADABLE = "declared source unreadable"

#: The file was read but is not valid TOML/JSON, or decodes to the wrong shape.
DEGRADED_MALFORMED = "declared source malformed"

#: The document parsed but does not carry the values that were sought.
DEGRADED_INCOMPLETE = "declared source incomplete"

#: Rendered around the joined reasons by
#: :func:`describe_configuration_degradation`.
_DEGRADED_PREFIX = "health configuration degraded: "
_DEGRADED_SUFFIX = "; serving the compiled-in fallback values"

# ---------------------------------------------------------------------------
# Declared configuration sources
#
# Resolved from this module's own location, never from the current working
# directory, so the values are identical wherever the server is launched from
# -- the repository root, a parent directory, or any other working directory.
# ---------------------------------------------------------------------------

MODULE_DIR = Path(__file__).resolve().parent

#: Identity source: ``[project] name`` and ``[project] version``.
PYPROJECT_PATH = MODULE_DIR / "pyproject.toml"

#: Serving source: ``host``, ``port`` and ``path``.  The document also declares
#: the ``status`` literal, which this module never reads -- see
#: :data:`STATUS_UP` -- and which the test suite pins to that constant instead.
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


def _classify_load_failure(error):
    """Map a load exception to one of this module's fixed reason constants.

    The exception itself is deliberately discarded after classification.  Its
    message would name the path that failed and, for a decode error, quote the
    offending bytes of the file -- exactly the configuration data a diagnostic
    must not disclose.  What an operator actually needs is the category, and the
    category is all that is kept.

    ``FileNotFoundError`` and ``NotADirectoryError`` are tested before their
    ``OSError`` base class, because both are subclasses and the order of
    ``except`` clauses is not available to a plain function.

    :param error: the exception a loader caught.
    :returns: one of :data:`DEGRADED_MISSING`, :data:`DEGRADED_UNREADABLE`,
        :data:`DEGRADED_MALFORMED` or :data:`DEGRADED_INCOMPLETE`.
    """
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return DEGRADED_MISSING
    if isinstance(error, OSError):
        return DEGRADED_UNREADABLE
    if isinstance(error, ValueError):
        # ``tomllib.TOMLDecodeError``, ``json.JSONDecodeError`` and
        # ``UnicodeDecodeError`` are all ``ValueError`` subclasses.
        return DEGRADED_MALFORMED
    # ``KeyError`` (an absent table or key) or ``TypeError`` (a ``project``
    # entry that is not a table): the document parsed, but not into the shape
    # the loader needed.
    return DEGRADED_INCOMPLETE


def _read_identity(path):
    """Read identity from a ``pyproject.toml``.  Never raises.

    ``tomllib.load`` requires a binary stream, hence ``"rb"``.  Every failure
    mode still collapses to the literal fallbacks -- a missing or unreadable
    file (``OSError``), malformed TOML (``tomllib.TOMLDecodeError``, a
    ``ValueError`` subclass), an absent ``[project]`` table or key
    (``KeyError``), or a document whose ``project`` entry is not a table
    (``TypeError``) -- but the *reason* is returned alongside them instead of
    being discarded, so the fallback is distinguishable from a successful load.

    Provenance is reported per value rather than per file, because the two are
    not the same thing: a document can load cleanly and still supply a blank or
    wrongly-typed ``name``, in which case that one value falls back while the
    file itself did not fail.

    :param path: the ``pyproject.toml`` to read.
    :returns: ``((name, name_source), (version, version_source), reason)``, where
        each source is :data:`FROM_FILE` or :data:`FROM_FALLBACK` and ``reason``
        is ``None`` on the ordinary path and otherwise one of this module's fixed
        reason constants.  Neither ever carries a path or a file's contents.
    """
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        name = project["name"]
        version = project["version"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return (
            (FALLBACK_NAME, FROM_FALLBACK),
            (FALLBACK_VERSION, FROM_FALLBACK),
            _classify_load_failure(error),
        )
    return (
        _labelled_text(name, FALLBACK_NAME),
        _labelled_text(version, FALLBACK_VERSION),
        None,
    )


def _labelled_text(declared, fallback):
    """Return ``(value, source)`` for one declared text value.

    A blank or wrongly-typed declaration is treated as absent, exactly as
    :func:`_coerce_text` treats it, and labelled :data:`FROM_FALLBACK` so the
    substitution is visible rather than implied by the value alone.
    """
    value = _coerce_text(declared, None)
    if value is None:
        return fallback, FROM_FALLBACK
    return value, FROM_FILE


def _read_serving_document(path):
    """Read ``config/health.json`` into ``(mapping, reason)``.  Never raises.

    Returns an empty mapping on any failure -- missing or unreadable file
    (``OSError``), malformed JSON (``json.JSONDecodeError``, a ``ValueError``
    subclass), undecodable bytes (``UnicodeDecodeError``, likewise a
    ``ValueError``), or a document whose top level is not an object -- so that
    every individual value then resolves to its literal fallback.  As with the
    identity loader, the reason is returned rather than swallowed.

    A top level that parses but is not an object is reported as *malformed*
    rather than *incomplete*: the file is syntactically valid but structurally
    wrong for its purpose, and no amount of key lookup could succeed against it.

    :param path: the ``config/health.json`` to read.
    :returns: ``(document, reason)``, with ``reason`` ``None`` on the ordinary
        path.
    """
    try:
        with open(path, "rb") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as error:
        return {}, _classify_load_failure(error)
    if isinstance(document, dict):
        return document, None
    return {}, DEGRADED_MALFORMED


def _resolve_host(document):
    """Resolve the bind address: ``HEALTH_HOST`` -> config -> literal.

    :returns: ``(value, source)``, where ``source`` is one of
        :data:`FROM_ENVIRONMENT`, :data:`FROM_FILE` or :data:`FROM_FALLBACK`.
    """
    override = _coerce_text(os.environ.get(ENV_HOST), None)
    if override is not None:
        return override, FROM_ENVIRONMENT
    declared = _coerce_text(document.get("host"), None)
    if declared is not None:
        return declared, FROM_FILE
    return FALLBACK_HOST, FROM_FALLBACK


def _resolve_port(document):
    """Resolve the bind port: ``HEALTH_PORT`` -> config -> literal.

    An override that is set but unusable (``HEALTH_PORT=abc``, ``=99999``,
    ``=0``) does not short-circuit to the literal; it falls through to the
    configuration file first, so the chain degrades one link at a time.

    :returns: ``(value, source)``, labelled as in :func:`_resolve_host`.
    """
    override = _coerce_port(os.environ.get(ENV_PORT), None)
    if override is not None:
        return override, FROM_ENVIRONMENT
    declared = _coerce_port(document.get("port"), None)
    if declared is not None:
        return declared, FROM_FILE
    return FALLBACK_PORT, FROM_FALLBACK


def _resolve_frozen_value(document, key):
    """Resolve a contract-frozen value, adopting a declaration only if it is legal.

    This is what makes ``path`` and ``status`` genuinely *resolved settings*
    rather than either dead declarations or free ones.  ``config/health.json`` is
    the declared source for both -- it carries them so an operator sees the whole
    shape of what is served in one place -- so the value that routes and the value
    that is reported are read from that source like every other setting.  What
    differs is the validation: the contract admits exactly one legal value for
    each, so a declaration is adopted only when it matches that value exactly,
    and anything else is *rejected* rather than honoured.

    A rejection never refuses to serve.  The frozen literal is used, so the
    endpoint keeps answering the contract; the rejection is recorded by
    :func:`_find_frozen_conflicts` and reported once at start-up; and the
    returned label is :data:`FROM_FALLBACK`, so neither an operator nor a test can
    mistake a rejected declaration for an honoured one.

    :param document: the parsed serving document, or an empty mapping.
    :param key: ``"path"`` or ``"status"`` -- a key of :data:`_FROZEN_BY_KEY`.
    :returns: ``(frozen_literal, source)``, where ``source`` is
        :data:`FROM_FILE` when the declaration was read and adopted and
        :data:`FROM_FALLBACK` when it was absent, blank, of the wrong type or
        rejected.
    """
    frozen = _FROZEN_BY_KEY[key]
    declared = _coerce_text(document.get(key), None)
    if declared == frozen:
        return frozen, FROM_FILE
    return frozen, FROM_FALLBACK


def _find_frozen_conflicts(document):
    """Return one descriptor per declared value that redefines a frozen constant.

    The companion to :func:`_resolve_frozen_value`: that function decides what is
    served, and this one records what was refused, so a rejection can be reported
    rather than swallowed.

    A key that is absent produces nothing, and a key that restates the frozen
    value exactly produces nothing either: ``config/health.json`` declares both
    on purpose, so that an operator reading that file sees the whole shape of
    what is served in one place.  A value of the wrong type is a conflict too --
    it is unmistakably an attempt to set the value, and it is not the frozen
    literal.

    Pure by design: no file is read, nothing is written, nothing is logged and
    nothing is raised, so it is safe to call at import time.  Reporting belongs
    to ``server.py``, which owns every line this tier prints.

    Returns a tuple of ``(key, configured, frozen)`` triples, in the order the
    keys appear in :data:`_FROZEN_BY_KEY`.
    """
    if not isinstance(document, dict):
        return ()

    conflicts = []
    for key, frozen in _FROZEN_BY_KEY.items():
        if key not in document:
            continue
        declared = document[key]
        configured = declared.strip() if isinstance(declared, str) else str(declared)
        if configured != frozen:
            conflicts.append((key, configured, frozen))
    return tuple(conflicts)

def declared_status(document):
    """Return the ``status`` a serving document declares, or ``None``.

    Read for inspection, separately from resolution.  What is *served* comes from
    :func:`_resolve_frozen_value`, which validates the declaration before adopting
    it; this function reports the raw declaration, so a consumer -- the test suite,
    or an operator reading the resolved configuration -- can compare what a file
    asked for against what was served without re-implementing the read.

    A document that declares nothing, or declares a blank value, yields
    ``None``: the same treatment every other setting gives a blank declaration.
    """
    return _coerce_text(document.get("status"), None)


# ---------------------------------------------------------------------------
# One-time resolution
#
# Executed exactly once, when this module is first imported.  The request
# handler never re-reads a file: a health probe must stay lightweight, and a
# probe that performs per-request I/O becomes a source of the very load it
# exists to report on.  Only the clock is read per request.

_SERVING_DOCUMENT, _SERVING_DEGRADATION = _read_serving_document(CONFIG_PATH)

#: Application identity, from ``pyproject.toml`` with literal fallbacks.  There
#: is no environment override for either value: identity is declared by the
#: repository, not by the deployment.
(
    (APP_NAME, _NAME_SOURCE),
    (APP_VERSION, _VERSION_SOURCE),
    _IDENTITY_DEGRADATION,
) = _read_identity(PYPROJECT_PATH)

#: Why each declared source was not used, in declared order -- empty on the
#: ordinary path.  Each entry is ``"<category>: <reason>"``, assembled purely
#: from this module's own constants.
#:
#: Nothing is written to either stream here.  Importing this module must stay
#: byte-silent: a library that logs on import corrupts the output of any program
#: that merely reads its constants, and this tier's contract allows the server
#: process exactly one line of ordinary output.  Reporting is therefore the
#: entry point's job -- ``server.py`` renders
#: :func:`describe_configuration_degradation` once, at start-up -- while this
#: module's only responsibility is to make the fallback *knowable*.
CONFIG_DEGRADATIONS = tuple(
    f"{category}: {reason}"
    for category, reason in (
        (SOURCE_IDENTITY, _IDENTITY_DEGRADATION),
        (SOURCE_SERVING, _SERVING_DEGRADATION),
    )
    if reason is not None
)

#: Resolved serving parameters.  ``server.py`` consumes these rather than
#: duplicating the defaults, so there is exactly one place in this tier where
#: the precedence chain is applied.
HOST, _HOST_SOURCE = _resolve_host(_SERVING_DOCUMENT)
PORT, _PORT_SOURCE = _resolve_port(_SERVING_DOCUMENT)

#: The one path served and the one status reported, both resolved from
#: ``config/health.json`` and both validated against the contract before the
#: declaration is adopted, so a declared value can never move the endpoint or
#: change what it claims.  See :func:`_resolve_frozen_value`.
HEALTH_PATH, _PATH_SOURCE = _resolve_frozen_value(_SERVING_DOCUMENT, "path")
HEALTH_STATUS, _STATUS_SOURCE = _resolve_frozen_value(_SERVING_DOCUMENT, "status")

#: Which link of its chain supplied each resolved value, keyed exactly as
#: :func:`get_config`.  See the provenance-label block above for why this is
#: exported rather than inferred from the values themselves.
CONFIG_SOURCES = {
    "name": _NAME_SOURCE,
    "version": _VERSION_SOURCE,
    "host": _HOST_SOURCE,
    "port": _PORT_SOURCE,
    "path": _PATH_SOURCE,
    "status": _STATUS_SOURCE,
}

#: Declared values that were rejected for redefining a frozen constant, computed
#: once at import.  Empty for the configuration this repository ships, because
#: ``config/health.json`` restates both frozen values exactly.  A non-empty tuple
#: means the deployed configuration tried to redefine the contract: the endpoint
#: still serves the frozen values -- that is the whole point -- and ``server.py``
#: prints one warning line per entry at start-up, so the mismatch reaches whoever
#: caused it instead of surfacing later as a gap in monitoring.
FROZEN_CONFLICTS = _find_frozen_conflicts(_SERVING_DOCUMENT)


def frozen_value_conflicts():
    """Return :data:`FROZEN_CONFLICTS`, the rejected frozen-value declarations.

    Provided as a function as well as a constant so that a consumer reads the
    audit through a stable call rather than a module attribute it might be
    tempted to rebind.

    >>> frozen_value_conflicts()
    ()
    """
    return FROZEN_CONFLICTS

def describe_configuration_degradation():
    """Describe a degraded configuration in one line, or ``None`` if healthy.

    Called once by ``server.py`` at start-up.  Returning ``None`` on the
    ordinary path is what keeps the process's output to the single announced
    line the contract expects: there is nothing to say when both declared
    sources loaded, and a "configuration OK" line every run would train a reader
    to ignore the one run where it mattered.

    The returned text is built only from this module's constants, so it can
    disclose neither a path, nor a file's contents, nor an environment value,
    nor a credential -- and it is a single line with no caller-controlled text,
    so it cannot forge additional log lines.

    >>> describe_configuration_degradation() is None or True
    True

    :returns: a one-line diagnostic, or ``None`` when nothing degraded.
    """
    if not CONFIG_DEGRADATIONS:
        return None
    return _DEGRADED_PREFIX + ", ".join(CONFIG_DEGRADATIONS) + _DEGRADED_SUFFIX


def get_config():
    """Return the resolved configuration as a fresh, independent ``dict``.

    A copy is returned on every call so that a caller -- ``server.py`` when it
    logs the bound address, or the test suite when it asserts the resolution
    chain -- cannot mutate the module's resolved state.

    Every value the endpoint serves appears here, including the two validated
    ones: each traces to a declared source rather than to a literal written
    inline at its point of use.  :func:`get_config_sources` says which link of
    each chain supplied the value.

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
        "status": HEALTH_STATUS,
    }


def get_config_sources():
    """Return a fresh copy of :data:`CONFIG_SOURCES`, keyed as :func:`get_config`.

    Copied for the same reason the configuration is: a caller must not be able to
    rewrite the module's record of where its own values came from.

    >>> sorted(get_config_sources())
    ['host', 'name', 'path', 'port', 'status', 'version']
    >>> set(get_config_sources().values()) <= {'environment', 'file', 'fallback'}
    True
    """
    return dict(CONFIG_SOURCES)


# Payload construction.


def _next_timestamp_millis():
    """Allocate the millisecond value the next timestamp will carry.

    The wall clock is read on **every** call, so the value tracks real time and
    is never captured at import or cached.  It is then forced to be *strictly
    greater* than the previous value handed out, which is what makes the
    contract's freshness clause hold literally: two consecutive responses always
    differ in ``timestamp``, including two that arrive inside the same
    millisecond and two that straddle a backwards clock adjustment.

    Why that matters rather than being a nicety: freshness is the endpoint's
    proof of *liveness* rather than of mere reachability.  A process frozen after
    binding its socket would keep serving a well-formed payload, and comparing
    two consecutive responses is how a poller detects it -- so "the two
    responses happened to share a millisecond" must never be indistinguishable
    from "the process stopped moving".  A raw clock read cannot make that
    distinction, because this contract's precision is milliseconds and two
    probes can easily land in one.

    The correction is bounded and self-cancelling.  It advances the issued value
    by one millisecond per call only while calls arrive faster than the clock
    ticks, and the moment real time catches up the wall clock wins again, so the
    value can never drift persistently ahead of it.  An endpoint answering a
    poller every few seconds never enters that regime at all.

    The lock is required, not defensive: ``server.py`` serves on
    :class:`~http.server.ThreadingHTTPServer`, so two handler threads can ask
    for a timestamp at the same moment, and a read-then-write without the lock
    would let them interleave and hand out the same value twice.

    Integer arithmetic throughout -- ``time.time_ns()`` reduced to milliseconds
    -- so no float rounding can ever move a value onto the wrong millisecond.
    """
    global _last_issued_millis

    now = time.time_ns() // _NANOSECONDS_PER_MILLISECOND
    with _timestamp_lock:
        _last_issued_millis = max(now, _last_issued_millis + 1)
        return _last_issued_millis


def format_timestamp(epoch_millis):
    """Render ``epoch_millis`` in the contract's timestamp form.

    The mandated form is ``YYYY-MM-DDTHH:MM:SS.mmmZ``: a ``Z`` suffix rather
    than a numeric offset, and exactly three fractional digits that are never
    dropped even when they are zero.

    ``datetime.isoformat()`` cannot be used: it emits six fractional digits and
    ``+00:00``.  The seconds and the milliseconds are split from one integer, so
    the two halves can never come from different instants.

    Separated from :func:`current_timestamp` so the whole-second case -- the one
    a naive formatter silently breaks -- can be asserted deterministically
    instead of waiting for the clock to land on one.

    >>> format_timestamp(1_000)
    '1970-01-01T00:00:01.000Z'
    """
    seconds, milliseconds = divmod(int(epoch_millis), _MILLISECONDS_PER_SECOND)
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    return f"{moment.strftime(_TIMESTAMP_SECONDS_FORMAT)}.{milliseconds:03d}Z"


def current_timestamp():
    """Return the current UTC time as RFC 3339 with millisecond precision.

    Fresh on every call and strictly later than every timestamp this process has
    returned before -- see :func:`_next_timestamp_millis` for why the second half
    of that guarantee is part of the contract rather than an implementation
    detail.
    """
    return format_timestamp(_next_timestamp_millis())


def build_payload():
    """Build the frozen four-member health payload.  Socket-free.

    Socket-free and side-effect-free: callable directly by the test suite with
    no listener bound.  ``name``, ``version`` and ``status`` are the values
    resolved once at import; only ``timestamp`` is evaluated here.  Dictionaries
    preserve insertion order and ``json.dumps`` honours it, so the insertion
    order below *is* the wire order (see :data:`PAYLOAD_KEYS`).

    :data:`HEALTH_STATUS` is guaranteed to be :data:`STATUS_UP` -- its resolution
    validates the declaration against the contract before adopting it -- so no
    configuration file and no environment variable can alter what this endpoint
    reports about itself, while the value still traces to a declared source.

    ``timestamp`` is evaluated here on every call -- never cached, memoized or
    captured at start-up -- because that is what makes the endpoint proof of
    liveness rather than proof of reachability: a process frozen after binding
    its socket would otherwise keep serving a stale but well-formed payload.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "timestamp": current_timestamp(),
        "status": HEALTH_STATUS,
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


#: Pre-serialized error bodies: constant, so built once at import rather than
#: per rejected request, and compact for the same byte-shape reason as the
#: success body.  Measured lengths 21 and 30 bytes.
NOT_FOUND_BODY = serialize({"error": "Not Found"})
METHOD_NOT_ALLOWED_BODY = serialize({"error": "Method Not Allowed"})


# ---------------------------------------------------------------------------
# Handler failure reporting
#
# Two very different things can go wrong while answering a request, and
# collapsing them into one silent branch is what made an internal defect
# indistinguishable from an ordinary disconnect.
#
# A peer that goes away mid-response -- ``BrokenPipeError``,
# ``ConnectionResetError``, a read timeout, any other ``OSError`` -- is the
# client's business and not a fault of this application.  A poller that stops
# waiting is normal, happens constantly, and must stay silent: reporting it
# would put one line in the log per aborted probe, which is precisely the
# per-request noise the contract forbids.
#
# Anything else is a defect in this program.  It cannot be diagnosed from the
# endpoint, because the contract defines no ``5xx`` response and nothing about
# the failure is ever reflected to the client, so if it is not reported
# server-side it is invisible -- the symptom would be a connection that closes
# with no explanation anywhere.
#
# Reporting is therefore latched: the first occurrence of each distinct
# exception category writes one line, and every repetition is silent.  A
# recurring defect under load cannot flood the log, and the bound below caps the
# total even against an adversarial variety of failures.
# ---------------------------------------------------------------------------

#: Rendered around the failure category.  Fixed literals, like every other
#: diagnostic in this module.
HANDLER_FAILURE_PREFIX = "health request handler failed: "
HANDLER_FAILURE_SUFFIX = "; connection closed without a response"

#: Most distinct categories that will ever be reported.  A latch already makes
#: repetition free; this bounds the number of *different* categories, so the set
#: below cannot grow without limit.
MAX_REPORTED_HANDLER_FAILURES = 8

#: Longest category name rendered.  A name is an identifier in every realistic
#: case; the cap is belt-and-braces against a pathological ``__name__``.
_MAX_CATEGORY_LENGTH = 64

#: Categories already reported.  Guarded by :data:`_FAILURE_LOCK` because the
#: server is threaded: without it, two threads failing simultaneously could both
#: pass the membership test and emit the same line twice.
_REPORTED_HANDLER_FAILURES = set()
_FAILURE_LOCK = threading.Lock()


def _sanitize_category(name):
    """Reduce an exception class name to safe, bounded, printable text.

    An exception category is the only caller-influenced text this module ever
    writes, so it is the only place a control character could conceivably reach
    a log line and forge a second entry.  In practice a class ``__name__`` is a
    Python identifier and passes through unchanged; a pathological one is
    stripped to the characters an identifier may contain and truncated.

    :param name: the candidate category name.
    :returns: non-empty text drawn only from letters, digits, ``_`` and ``.``.
    """
    if not isinstance(name, str):
        return "Unknown"
    kept = "".join(
        character
        for character in name[:_MAX_CATEGORY_LENGTH]
        if character.isalnum() or character in "_."
    )
    return kept or "Unknown"


def reported_handler_failures():
    """Return the failure categories reported so far, as a ``frozenset``.

    Exposed so the test suite can assert *that* an internal failure was
    signalled, and that an ordinary disconnect was not, without capturing
    standard error.  A snapshot is returned rather than the live set, so a
    caller cannot mutate this module's state.
    """
    with _FAILURE_LOCK:
        return frozenset(_REPORTED_HANDLER_FAILURES)


def _report_handler_failure(error):
    """Report an unexpected handler failure once per category.  Never raises.

    :param error: the exception that escaped a responder.
    :returns: ``True`` if this call emitted a line, ``False`` if the category was
        already reported or the bound has been reached.
    """
    category = _sanitize_category(type(error).__name__)
    with _FAILURE_LOCK:
        if category in _REPORTED_HANDLER_FAILURES:
            return False
        if len(_REPORTED_HANDLER_FAILURES) >= MAX_REPORTED_HANDLER_FAILURES:
            return False
        _REPORTED_HANDLER_FAILURES.add(category)
    try:
        print(
            HANDLER_FAILURE_PREFIX + category + HANDLER_FAILURE_SUFFIX,
            file=sys.stderr,
            flush=True,
        )
    except (OSError, ValueError):
        # Standard error is closed or its pipe has gone. Failing to report a
        # fault must never itself become one, and the category stays recorded so
        # :func:`reported_handler_failures` remains accurate either way.
        pass
    return True
# Request-target routing
#
# The contract admits exactly one route, matched by exact comparison, so the
# path a request asks for has to be derived from the target the client actually
# transmitted rather than from a parser's normalized rendering of it.  These
# four values are the whole vocabulary that derivation needs; they are private
# because they are the mechanics of the rule, not terms of the contract.
# ---------------------------------------------------------------------------

#: An origin-form target starts with this, and only such a target can match.
_ABSOLUTE_PATH_PREFIX = "/"

#: Everything from the first of these onwards is discarded, and it is the only
#: thing ever discarded, because ``/health?probe=1`` must match while
#: ``/health/`` and ``/health#x`` must not.
_QUERY_DELIMITER = "?"


def request_target_path(target):
    """Return the path component of *target*, or ``None`` if it cannot match.

    The contract's normative path rule, implemented identically by all three
    tiers.  Exposed as a plain function -- rather than living only inside the
    handler -- so the rule can be asserted directly, with no socket and no
    listener, exactly as the sibling tiers expose theirs.

    The rule is deliberately the most restrictive one that still satisfies the
    contract, in three steps:

    1. a target that is not a recoverable string beginning with ``/`` matches
       nothing.  That rejects asterisk-form (``*``), authority-form and
       absolute-form (``GET http://host/health HTTP/1.1``): absolute-form
       carries its own authority, so honouring it would make this server answer
       for any host name a caller chose to write, which is a second spelling of
       the one resource this endpoint serves;
    2. everything from the first ``?`` onwards is discarded, which is what makes
       ``/health?x=1`` the same resource as ``/health``;
    3. what remains is the path, taken verbatim.

    Nothing is decoded, no run of slashes is collapsed, no dot segment is
    resolved and no fragment is trimmed, so every alias a URI parser might
    otherwise fold onto ``/health`` answers ``404``.  See
    :meth:`HealthRequestHandler._requested_path` for why delegating this to a
    URI parser would give one documented contract three different sets of
    undocumented aliases.

    >>> request_target_path("/health?probe=1")
    '/health'
    >>> request_target_path("/%68ealth")
    '/%68ealth'
    >>> request_target_path("http://127.0.0.1:8000/health") is None
    True
    >>> request_target_path("/health#x")
    '/health#x'
    >>> request_target_path("*") is None
    True
    """
    if not isinstance(target, str) or not target.startswith(_ABSOLUTE_PATH_PREFIX):
        return None
    query_start = target.find(_QUERY_DELIMITER)
    return target if query_start < 0 else target[:query_start]


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


class HealthRequestHandler(BaseHTTPRequestHandler):
    """``BaseHTTPRequestHandler`` implementing the frozen health contract.

    Method first, then path, and the order is normative.  Anything other than
    ``GET`` or ``HEAD`` is answered ``405`` with ``Allow: GET, HEAD`` whatever
    the path -- so ``POST /unknown`` is a ``405``, not a ``404`` -- and only an
    accepted method reaches the path comparison.

    1. **Method first.**  Anything other than ``GET`` or ``HEAD`` is answered
       ``405`` with ``Allow: GET, HEAD``, whatever the path -- so ``POST
       /unknown`` is a ``405`` and not a ``404``.  A caller therefore always
       learns the most actionable fact first: that its method is not permitted
       anywhere on this server.
    2. **Then path, compared raw and exactly.**  The *raw* origin-form path of
       the request target -- the client's own bytes with only the query
       component removed -- is compared for equality against the frozen
       :data:`HEALTH_PATH`.  A query string is ignored entirely, so
       ``/health?x=1`` succeeds.  Nothing else is ignored: trailing slashes are
       not normalized, dot segments are not resolved, percent-encoded bytes are
       not decoded, a fragment is not trimmed and an absolute-form target is not
       accepted, so ``/health/``, ``/other/../health``, ``/%2e%2e/health``,
       ``/health#x`` and ``http://host/health`` are each a ``404``.  See
       :meth:`_requested_path` for why each of those matters.

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
    monkeypatching module state: :attr:`health_path` is read through the class
    rather than closed over, and the payload is rendered through the
    module-level :func:`render_payload`, so a test may substitute that function.
    Neither hook is a configuration surface -- both require a deliberate
    in-code subclass, which is exactly what a *configuration* document must not
    be able to accomplish.
    """

    #: Safe only because every response sets an accurate ``Content-Length``, so
    #: a persistent connection is always correctly framed.
    protocol_version = "HTTP/1.1"

    #: A liveness probe has no need to advertise the interpreter it runs on.
    #: Emptying ``sys_version`` suppresses the version and overriding
    #: :meth:`version_string` suppresses the separator space the base
    #: implementation would leave behind, so the header reads ``Server: health``.
    server_version = "health"
    sys_version = ""

    #: Idle timeout per accepted connection.  Without it, a client that opens a
    #: persistent connection and never completes a request would hold a worker
    #: thread for the life of the process.  A probe takes single-digit
    #: milliseconds, so ten seconds is generous.
    timeout = 10

    #: Send every response the instant it is written, without waiting for the
    #: peer to acknowledge the previous one.
    #:
    #: This is ``TCP_NODELAY``, and it is set because without it a *keep-alive*
    #: probe pays a delayed-acknowledgement penalty that has nothing to do with
    #: this application's work.  Measured on the reference host, three sequential
    #: requests on one connection: the first completed in 0.5 ms, the second and
    #: third in **40.8 ms and 41.1 ms** -- their headers arrived in 0.1-0.2 ms and
    #: the *body* then sat unsent for the remainder.  Nagle's algorithm was
    #: holding the second small segment of the response until the peer
    #: acknowledged the first, and the peer's stack was itself delaying that
    #: acknowledgement.  Two mechanisms, each individually reasonable, combining
    #: into a stall an order of magnitude larger than the response it delayed.
    #:
    #: :meth:`_send` removes the second segment (see :meth:`_flush_response`), and
    #: this flag removes the wait -- both, because they defend against different
    #: halves of the same interaction and only the pair is robust: a response that
    #: happened to exceed one segment would stall again with coalescing alone,
    #: and a response written in two segments is a wasted round trip even with
    #: ``TCP_NODELAY`` set.
    #:
    #: ``socketserver`` notes that this flag is intended for use with
    #: ``wbufsize != 0``, its buffered-writer mode, and that pairing is
    #: deliberately *not* adopted here.  Buffering would coalesce the writes as a
    #: side effect, but it also moves the socket write out of :meth:`_send` and
    #: into the base class's per-request ``wfile.flush()``, outside the
    #: ``OSError`` guard that keeps an ordinary peer disconnect from becoming a
    #: ``socketserver`` traceback on standard error.  Explicit coalescing keeps
    #: the write, and therefore the error handling, exactly where the contract's
    #: silence requirement needs it.
    #:
    #: The endpoint's responses are ~100-200 bytes and it performs no streaming,
    #: so the small-packet concern the flag exists to guard against cannot arise
    #: here: there is one write per response, and it is already one segment.
    disable_nagle_algorithm = True

    #: The path this handler answers on: the value resolved at import, which
    #: validation guarantees is the contract's literal.  Read through the class so
    #: a subclass can be pointed elsewhere in a test without mutating module
    #: state, which a configuration document cannot do.
    health_path = HEALTH_PATH

    #: Upper bound on a request body this handler will consume before giving up
    #: and closing the connection.  Nothing legitimate sends a body to this
    #: endpoint; the drain exists only to keep a persistent connection framed.
    _max_drain_bytes = 1 << 20
    _drain_chunk_bytes = 1 << 16

    #: Protocol-level errors are raised by the base class *before* routing ever
    #: happens -- a malformed request line (``400``), an over-long request
    #: target (``414``), too many header fields (``431``).  They are outside the
    #: contract, which describes routed responses, and the base class would
    #: otherwise answer them with a roughly half-kilobyte HTML page.  These are
    #: the two hooks it supports for replacing that, and they give the body the
    #: same compact single-member JSON shape as the contract's own error bodies.
    #:
    #: What they do NOT change, measured rather than assumed:
    #:
    #: * the base class adds ``Connection: close`` to every such response and
    #:   then closes the connection, which the contract's own responses never
    #:   do; and
    #: * when the request *line* is the thing that could not be parsed, no
    #:   protocol version was ever established, so the base class's
    #:   ``send_response_only``, ``send_header`` and ``end_headers`` are all
    #:   no-ops for HTTP/0.9 and the peer receives the JSON body ALONE -- no
    #:   status line and no headers.  That is deliberate standard-library
    #:   behaviour for a version-less request and is left alone: synthesising a
    #:   status line for a request that never declared a version would be worse
    #:   than sending none.
    #:
    #: See :meth:`send_error` for why the format interpolates only the reason
    #: phrase.
    error_content_type = CONTENT_TYPE
    error_message_format = '{"error":"%(message)s"}'

    # Method dispatch.

    # The do_* names are mandated by BaseHTTPRequestHandler's attribute-based
    # dispatch; they are not this module's naming choice.

    def do_GET(self):
        """Serve ``GET``: the payload on the health path, ``404`` elsewhere."""
        self._serve(write_body=True)

    def do_HEAD(self):
        """Serve ``HEAD``: identical status and headers to ``GET``, no body.

        The payload is still built and measured so that ``Content-Length``
        equals the byte length a ``GET`` would have produced.  A ``HEAD`` is
        therefore a valid, cheap liveness check that still proves the payload
        could be built.
        """
        self._serve(write_body=False)

    def do_POST(self):
        """Reject ``POST`` with ``405``.

        Spelled out explicitly because ``POST`` is the method a caller is most
        likely to try; every other disallowed method reaches the same responder
        through :meth:`__getattr__`.
        """
        self._respond_method_not_allowed()

    def __getattr__(self, name):
        """Route any other ``do_<METHOD>`` lookup to the ``405`` responder.

        ``BaseHTTPRequestHandler`` dispatches by attribute name and answers
        ``501`` when no matching ``do_*`` exists.  ``501`` is not the contract,
        and enumerating verbs would still leave exotic ones (``TRACE``,
        ``PROPFIND``, an invalid token) on ``501``, so the lookup itself is
        intercepted.  Only ``do_``-prefixed names are; anything else raises
        ``AttributeError`` as usual, keeping ``hasattr`` checks elsewhere in the
        standard library meaningful.  ``__getattr__`` runs only after normal
        lookup fails, so the methods defined above always win.
        """
        if name.startswith("do_"):
            return self._respond_method_not_allowed
        raise AttributeError(name)

    # Responders.

    def _serve(self, write_body):
        """Answer an allowed method: ``200`` on the health path, else ``404``.

        The ``404`` here is a genuine routing outcome -- the client asked for a
        path this server does not serve.  It is never used to paper over a
        failure while building the payload; that case reaches
        :meth:`_abandon_connection`, which reports it and closes, so an internal
        defect can never masquerade as an ordinary "no such path".
        """
        try:
            if self._requested_path() == self.health_path:
                self._send(STATUS_OK, render_payload(), write_body=write_body)
            else:
                self._send(
                    STATUS_NOT_FOUND, NOT_FOUND_BODY, write_body=write_body
                )
        # Deliberately broad: see _abandon_connection for why nothing may
        # escape a request handler here.
        except Exception as error:
            self._abandon_connection(error)

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
        # Deliberately broad: see _abandon_connection for why nothing may
        # escape a request handler here.
        except Exception as error:
            self._abandon_connection(error)

    def _send(self, status, body, write_body=True, allow=None):
        """Write one complete response: status line, contract headers, body.

        ``send_response`` also emits ``Server`` and ``Date``, both outside the
        contract.  ``OSError`` -- including ``BrokenPipeError``,
        ``ConnectionResetError`` and ``TimeoutError`` -- means the peer went
        away mid-response, which is the client's business and not this
        application's fault, so the connection is abandoned quietly.

        The whole response leaves in a single write: see :meth:`_flush_response`
        for why that is a correctness property of a keep-alive probe and not a
        micro-optimization.
        """
        try:
            self.send_response(status)
            if allow is not None:
                self.send_header("Allow", allow)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Cache-Control", CACHE_CONTROL)
            self.send_header("Content-Length", str(len(body)))
            self._flush_response(body if write_body else b"")
        except OSError as error:
            self._abandon_connection(error)

    def _flush_response(self, payload):
        """Emit the buffered head and ``payload`` in one write to the socket.

        The base class buffers the status line and every header, then
        :meth:`~http.server.BaseHTTPRequestHandler.end_headers` appends the blank
        line and flushes -- one write -- after which a caller writes the body,
        which on this handler's unbuffered ``wfile`` (``socketserver`` sets
        ``wbufsize = 0``) is a *second* write and therefore a second TCP segment.
        That second segment is what stalled behind Nagle and delayed
        acknowledgement for ~41 ms on every keep-alive request after the first;
        :attr:`disable_nagle_algorithm` records the measurement.

        Appending the body to the same buffer and flushing once produces the
        identical bytes in one segment.  ``flush_headers`` is used rather than a
        hand-rolled write because it is the base class's own documented flush: it
        joins the buffer, writes it, and -- importantly on a persistent connection
        -- clears it, so nothing can leak into the next response on the same
        socket.

        Two cases do not have a header buffer to append to, and both fall back to
        the base class's own sequence so that the bytes on the wire are exactly
        what they are today:

        * **An HTTP/0.9 request.**  A request line carrying no version leaves
          ``send_response_only``, ``send_header`` and ``end_headers`` as no-ops,
          so there is no status line and no header block -- the peer receives the
          body alone, which is all RFC 1945 §6 defines for a version-less
          request.  That is deliberate standard-library behaviour and is
          preserved verbatim.
        * **A buffer the base class did not create, or created as something other
          than a list.**  ``_headers_buffer`` is the standard library's private
          attribute.  It has had the same shape for the entire 3.x series, but
          this method does not require it: if the attribute is missing or is not a
          list, the response is written the long way instead.  A fallback costs a
          round trip; guessing wrong about a private attribute would cost the
          response.

        Either fallback still writes the correct response -- only in two segments
        rather than one, which is precisely the condition
        :attr:`disable_nagle_algorithm` also protects against.

        :param payload: the response body, or ``b""`` for a ``HEAD``.
        """
        buffered = getattr(self, "_headers_buffer", None)

        if self.request_version != "HTTP/0.9" and isinstance(buffered, list):
            buffered.append(b"\r\n")
            if payload:
                buffered.append(payload)
            self.flush_headers()
            return

        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _abandon_connection(self, error=None):
        """Fail closed: end this connection without raising and without a 5xx.

        Two things must not happen when a responder fails, and this method is
        how both are avoided.  The exception must not propagate, because
        ``socketserver`` would print a traceback and tear the connection down
        abruptly.  And the fault must not be dressed up as a routing outcome: no
        ``404`` and no ``2xx`` may be sent for it.

        Closing the connection is the whole of the correct *response* behaviour
        at this tier -- the socket is released and the next probe gets a clean
        one.  All three tiers do the same thing, which is the cross-tier
        internal-fault policy in ``docs/health-endpoint.md`` §9.3.1: report the
        fault server-side and complete the exchange at the transport level,
        inventing no status the contract does not define.  §5.1's matrix has
        exactly three rows -- ``200``, ``404``, ``405`` -- and none of them
        produces a ``5xx``, so none is synthesised here either.

        What closing cannot do is explain itself, which is why the cause is
        classified rather than discarded:

        * an ``OSError`` -- and its ``BrokenPipeError``,
          ``ConnectionResetError`` and ``TimeoutError`` subclasses -- means the
          peer went away.  Ordinary, frequent, none of this program's business,
          and reported nowhere.
        * anything else is a defect in this program.  Nothing about it reaches
          the client, so it is recorded server-side instead, once per category,
          by :func:`_report_handler_failure`.

        :param error: the exception that led here, or ``None`` when the caller
            has already established that the cause is an ordinary disconnect.
        """
        self.close_connection = True
        if error is not None and not isinstance(error, OSError):
            _report_handler_failure(error)

    # Helpers.

    def _raw_request_target(self):
        """Recover the request target exactly as the client transmitted it.

        :attr:`~http.server.BaseHTTPRequestHandler.requestline` is captured
        while the request line is first read, *before*
        :meth:`~http.server.BaseHTTPRequestHandler.parse_request` rewrites
        :attr:`path`, so the client's original bytes survive there even when
        :attr:`path` has been altered.  A request line is
        ``METHOD SP TARGET SP VERSION`` (HTTP/1.x) or ``METHOD SP TARGET``
        (HTTP/0.9); the target is the second whitespace-delimited field.

        Returns the raw target, or ``None`` when no target can be recovered.
        There is deliberately no fallback to :attr:`path`: that attribute is the
        rewritten form, so falling back to it would reintroduce exactly the alias
        this method exists to avoid.  An unrecoverable target matches no path and
        is answered with the contract's ``404`` -- the strictly safer outcome, and
        the reason :attr:`path` is not read anywhere in this module.
        """
        requestline = getattr(self, "requestline", None)
        if not isinstance(requestline, str):
            return None
        fields = requestline.split()
        if len(fields) < 2:
            return None
        return fields[1]

    def _requested_path(self):
        """Return the raw origin-form path asked for, or ``None`` if none can match.

        The client's own bytes are used, and the **only** thing removed from them
        is the query component -- everything from the first ``?`` -- because
        ``/health?x=1`` must match while ``/health/`` must not.  Nothing else is
        done to the target, and that restraint is the whole substance of this
        method:

        * **No URL parsing.**  :func:`urllib.parse.urlsplit` drops a fragment, and
          a general-purpose URL parser such as the WHATWG one also resolves dot
          segments and decodes percent-encoded bytes.  Under any of those,
          ``/health#x``, ``/other/../health`` and ``/%2e%2e/health`` collapse into
          ``/health`` and are served ``200``.  Each is a distinct request target
          that the contract requires to answer ``404``: the contract admits
          exactly one route, and every extra spelling of it is an undocumented
          alias that monitoring, access logs and any intermediary in the path see
          as a different resource from the real one.
        * **Origin-form only.**  A target that does not begin with ``/`` is
          absolute-form (``GET http://host/health HTTP/1.1``), authority-form or
          asterisk-form.  Absolute-form carries its own authority, so honouring it
          would make this server answer for any host name a caller chose to write.
          The request is still *accepted* -- it receives a well-formed,
          contract-defined response -- but its target does not name a resource
          this server serves, so that response is the contract's ``404``.
        * **The client's bytes, not** :attr:`path`.  CPython's
          :meth:`~http.server.BaseHTTPRequestHandler.parse_request` collapses a
          run of leading slashes in the target to a single slash (gh-87389), so a
          client asking for ``//health`` is presented to application code as
          ``/health``.  That rewrite exists to stop a handler which *redirects*
          from emitting a protocol-relative ``Location`` a client would read as an
          absolute URI; this handler never redirects and has no ``3xx`` branch at
          all, so the rewrite protects against nothing here while silently
          creating an alias.  Reading :meth:`_raw_request_target` sidesteps it
          entirely -- strictly more restrictive than the standard-library
          behaviour, and therefore unable to reintroduce the open-redirect class
          that behaviour guards against.

        A target that cannot be recovered at all yields ``None``, which equals no
        path, so it routes to the contract's ``404`` rather than raising.
        """
        return request_target_path(self._raw_request_target())

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
        character that JSON would need escaped.  The body is therefore always a
        valid, compact ``{"error":"<reason phrase>"}``.

        Two things about the *rest* of that response are the base class's and
        not this override's, both measured: it appends ``Connection: close`` and
        closes the connection, and when the request line itself was unparseable
        no protocol version was established, so the status line and headers are
        suppressed for HTTP/0.9 and the peer receives the JSON body alone.

        None of this is a ``5xx`` branch and none of it is a routed response:
        these codes come from the base class's request parsing, which runs
        before this handler sees a method or a path.  The contract's own
        responses never pass through here.
        """
        super().send_error(code)

    def version_string(self):
        """Return the ``Server`` header value: the product token, nothing else.

        The base implementation joins ``server_version`` and ``sys_version``
        with a space, which with ``sys_version`` emptied would leave a trailing
        one.
        """
        return self.server_version

    # Logging.

    def log_message(self, format, *args):
        """Silence per-request logging.

        The base implementation writes a line to standard error for every
        request, which would pollute CI output and the container log with one
        entry per health poll -- and the contract admits no per-request logging.
        ``log_request`` and ``log_error`` both funnel through this method, so
        overriding it alone silences every per-request line, including the
        timeout notice for an idle connection.  The signature matches the
        standard library exactly, shadowed builtin name included, so the
        override is a drop-in replacement.  Start-up logging belongs to the entry
        point, which announces its bound address once.

        What this deliberately does *not* silence is
        :func:`_report_handler_failure`, which writes at most one line per
        distinct internal-defect category for the lifetime of the process.  That
        is not per-request logging: ordinary traffic, including a client that
        disconnects mid-response, still produces nothing at all.  The
        distinction matters because a defect that is invisible at the endpoint --
        the contract has no ``5xx`` -- would otherwise be invisible everywhere.
        """


#: Documented aliases.  The class above is the canonical name; these exist so
#: that a consumer importing under either common spelling resolves the same
#: object rather than failing at import time.
HealthHandler = HealthRequestHandler
HealthCheckHandler = HealthRequestHandler
