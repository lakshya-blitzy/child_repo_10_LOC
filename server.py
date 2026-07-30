"""Health endpoint entry point for ``child_repo_10_LOC``.

Binds a TCP listener, serves the ``/health`` contract through the request
handler in the sibling ``health.py``, and shuts the listener down in an orderly
fashion when asked to stop.  It does nothing else.  Every decision about *what*
the endpoint answers -- the payload, the compact serialization, the header set,
the ``405``, the ``404`` -- belongs to ``health.py`` and is deliberately not
restated here, because duplicating a contract across two files is how
independently written tiers drift out of agreement.

This tier has two parallel entry points, and that split is the whole point:
``python app.py`` prints ``Hello Lakshya`` and exits, exactly as it always has;
``python server.py`` binds ``0.0.0.0:8000`` and stays alive.  A listener makes a
process long-lived, so the listener lives *here* and never on the default path.

Four invariants this module holds:

* **Nothing binds at import.**  Importing defines classes and functions and
  returns: no socket, no thread, no signal handler, nothing written to either
  stream.  That is what lets a test import the factory below and bind its own
  listener on an ephemeral port.
* **One line of output, once.**  The bound address is announced at start-up and
  flushed immediately so it survives a pipe.  Nothing is logged per request and
  nothing on shutdown.  There is no logging framework in this tier.
* **Prompt, orderly shutdown on ``SIGTERM`` and ``SIGINT``.**  Both stop the
  accept loop, release the socket and exit ``0`` with no traceback, so a stop
  that was asked for leaves no orphaned port binding behind.  See
  :func:`serve_until_signalled` for the deadlock the naive approach hits.
* **A failed bind is reported, never disguised**, and never worked around by
  moving to another port: every probe targets a known port, so a silent shift
  would turn a clear failure into a confusing one.
* **Concurrency is bounded, and saturation is defined.**  At most
  :data:`MAX_CONCURRENT_REQUESTS` connections are served at once; beyond that the
  accept loop waits briefly and then refuses -- closing the connection without a
  response and counting it -- so a burst of stalled clients cannot turn into
  unbounded threads.  See :class:`HealthHTTPServer`.

Standard library only -- ``http.server.ThreadingHTTPServer`` and nothing else.
Flask, FastAPI, Starlette, uvicorn, gunicorn and waitress were each considered
and rejected: any one would give this repository its first pip dependency.

The bound address resolves through ``environment variable -> configuration file
-> compiled-in literal``, applied in exactly one place in this tier
(``health.py``, at import).  This module *consumes* :data:`health.HOST` and
:data:`health.PORT` rather than re-deriving them, so the two cannot disagree
about a default.  The overrides here are ``HEALTH_HOST`` and ``HEALTH_PORT``;
the bare ``HOST`` and ``PORT`` belong to the apex application and are
deliberately not read.  ``0.0.0.0`` is a bind address only -- it means "every
local interface" and is never a destination, so probe ``127.0.0.1``::

    python server.py
    curl -i http://127.0.0.1:8000/health
    HEALTH_PORT=8100 HEALTH_HOST=127.0.0.1 python server.py

Nothing here references another tier: no import crosses a tier boundary in
either direction, and nothing in this repository reads the shared contract
document at run time.
"""

import errno
import signal
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

# Tier-local import bootstrap.  ``health`` sits beside this file.  Launching a
# script by path already puts that directory on ``sys.path``, but ``python -m
# server`` resolves against the current working directory instead, and an
# embedded interpreter may start with a path that omits it entirely.  Deriving
# the directory from ``__file__`` makes every route behave identically wherever
# the process is launched from.  The insert is idempotent and additive, so a
# caller that arranged its own path ordering keeps it.

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

# Deliberately after the bootstrap above: the import cannot resolve before it.
import health

__all__ = [
    # The listener.
    "HealthHTTPServer",
    # Declared defaults and process-level constants.
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_CONCURRENT_REQUESTS",
    "ADMISSION_WAIT_SECONDS",
    "SHUTDOWN_SIGNALS",
    "EXIT_OK",
    "EXIT_BIND_FAILURE",
    "EXIT_SHUTDOWN_FAILURE",
    "UNSAFE_HOST_TEXT",
    # Behaviour, in the order the entry point uses it.
    "report_frozen_value_conflicts",
    "resolve_bind_address",
    "create_server",
    "startup_line",
    "serve_until_signalled",
    "main",
]

# Declared defaults, re-exported from ``health`` rather than spelled again, so
# "the default port is 8000" is stated in exactly one place in this tier.

#: Bind address of last resort: every local interface.
DEFAULT_HOST = health.FALLBACK_HOST

#: Bind port of last resort.  Each tier owns a distinct default port (3000 for
#: Level 1, 8000 here, 8080 for Level 3) because all three applications may run
#: simultaneously on one host; reusing another tier's port would collide.
DEFAULT_PORT = health.FALLBACK_PORT

#: The signals that mean "stop serving".  ``SIGTERM`` is the conventional
#: request to terminate; ``SIGINT`` is Ctrl-C at a terminal.  Both are handled
#: identically and both exit ``0``: a stop that was asked for is a success.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

#: Exit status after an orderly shutdown.
EXIT_OK = 0

#: Exit status when the listener could not be bound.  Deliberately non-zero and
#: deliberately *not* accompanied by a fallback to some other port: a probe that
#: targets 8000 must fail loudly if 8000 could not be taken.
EXIT_BIND_FAILURE = 1

#: Exit status when the listener served correctly but could not be shut down
#: cleanly.  A distinct code, because the two failures call for opposite
#: responses: a bind failure means nothing ever served, while this means
#: something served and may still hold the port, so a supervisor about to restart
#: this process needs to know the socket might not be free yet.
#:
#: The value ``2`` carries no special meaning for a process exit status.  It is
#: reserved by convention for a container health-check *probe command*, which
#: must return exactly 0 or 1 -- and this module is never a probe: a probe would
#: be a separate one-shot client, not the server itself.
EXIT_SHUTDOWN_FAILURE = 2

#: Accepted connections this listener will serve at once.
#:
#: A bound is mandatory rather than a refinement, and the reason is the handler's
#: own idle timeout: this tier speaks HTTP/1.1 with a ten-second idle timeout per
#: connection, so a client that connects and then stalls occupies a thread for up
#: to ten seconds while doing nothing.  Unbounded admission converts a burst of
#: such clients directly into unbounded threads -- each with its own stack and
#: file descriptor -- and the first symptom is not a slow endpoint but a process
#: that cannot allocate a thread at all.  A liveness endpoint failing that way is
#: the worst outcome available: it stops answering while nothing is wrong with
#: what it answers.  ``daemon_threads`` and a deep accept queue do not help here;
#: neither caps *work*, they only decide who waits and who is reaped.
#:
#: The value is chosen against the burst this tier is actually measured under.
#: The accept-queue assertion in the sibling test suite opens **40** simultaneous
#: connections and requires every one of them to be answered, so a cap at or
#: below that number would throttle a poll burst this endpoint is expected to
#: absorb.  Sixty-four sits above it with headroom while still bounding the
#: process to a commitment it can always meet.  It is a class attribute, so a
#: subclass -- a test, or a deployment with a different profile -- may lower or
#: raise it without editing this module.
MAX_CONCURRENT_REQUESTS = 64

#: How long the accept loop waits for a permit before refusing the connection.
#:
#: Not zero, because a burst that merely *arrives* faster than it is served is
#: normal for a polled resource and must not be refused: a brief wait absorbs it.
#: Not long either, and that is the more important half.  This wait is taken *in
#: the accept loop*, so every millisecond of it is a millisecond the listener is
#: not accepting anyone -- which is the same stall that ruled out serving a
#: refused connection on the caller's thread (see :meth:`process_request`).  A
#: generous wait would answer thread exhaustion by introducing an accept-loop
#: stall instead, and under sustained overload it would make refusals trickle out
#: at a fixed rate while everyone behind them queued in the kernel with no signal
#: at all.  A refused caller can retry at once; a queued caller can only wait.
#:
#: Twenty-five milliseconds is derived from measurement.  Sixty-four simultaneous
#: exchanges against this listener complete in 19-30 ms end to end -- a permit
#: freed every 0.3-0.5 ms, with the slowest single exchange at ~13 ms -- so this
#: wait is roughly two of the slowest requests and fifty of the average ones, and
#: a healthy micro-burst just above the ceiling is absorbed without a refusal.
#: What it deliberately does *not* absorb is the pathological case: a client that
#: connects and stalls holds its permit for up to the handler's ten-second idle
#: timeout, which no admission wait could ever wait out, so it is refused after
#: 25 ms rather than after half a second.  The accept loop is therefore never
#: stalled for more than 25 ms per refused connection, and a probe arriving during
#: genuine saturation gets a definite answer -- a closed connection -- far inside
#: its own three-second timeout.
ADMISSION_WAIT_SECONDS = 0.025

#: How long :meth:`~socketserver.BaseServer.serve_forever` waits between checks
#: of its shutdown flag.  A signal interrupts the wait immediately (PEP 475
#: retries the call with the *remaining* timeout), so this value bounds how long
#: the loop can take to notice a shutdown request -- a fifth of a second, which
#: keeps a requested stop prompt while costing five idle wake-ups a second.
_POLL_INTERVAL = 0.2

#: Upper bound on the wait for the shutdown helper thread to finish.  It exists
#: only so that a pathological state can never turn "stopping" into "hanging";
#: in practice the join returns in microseconds.  The thread is a daemon, so
#: even an expired join cannot keep the interpreter alive.
_SHUTDOWN_JOIN_TIMEOUT = 5.0

#: Prefix for diagnostics on standard error, so a message in an interleaved log
#: is attributable.  Derived from this file's own name rather than from
#: ``sys.argv[0]``, which an embedding process controls and may leave empty.
_PROGRAM = Path(__file__).name

# ---------------------------------------------------------------------------
# Diagnostic sanitization
#
# Every value this process writes to a log is either one of its own literals, a
# stable error category, or a host and port that came from configuration.  The
# last of those is the only caller-controlled text in the set, and a log is a
# sink an attacker or a careless value can pollute: one newline is enough to
# forge an additional entry that a human or a parser then trusts.  The constants
# below define what a renderable host may contain, and what is printed instead
# when it does not qualify.
# ---------------------------------------------------------------------------

#: Rendered in place of a host that is not safe or not usable to print.  The
#: same token is used by every tier of this composition, so an operator can grep
#: one string across all three logs.
UNSAFE_HOST_TEXT = "<unprintable>"

#: Rendered in place of a port that is not a number.
UNSAFE_PORT_TEXT = "<unprintable>"

#: Longest host text rendered.  Comfortably above the 253-octet maximum of a DNS
#: name is unnecessary here: a bind address in this composition is a literal or a
#: short name, and a bound protects the log line's legibility.
_MAX_HOST_LENGTH = 64

#: Longest configured value rendered inside a frozen-value warning.  A rejected
#: value is quoted so an operator can find it in the document, which needs its
#: leading characters and not all of them: a document may declare a value of any
#: length, and an unbounded one would put that length on the error stream at
#: every start-up.  Matches the bound the other tiers of this composition apply.
_MAX_CONFIGURED_LENGTH = 64

#: Appended to a configured value that was cut at :data:`_MAX_CONFIGURED_LENGTH`,
#: so a reader can tell a truncated rendering from a complete one.
_TRUNCATION_MARK = "..."

#: Characters a renderable host may contain: a DNS name, an IPv4 literal, an
#: IPv6 literal, or an IPv6 literal with a zone identifier such as
#: ``fe80::1%eth0``.  Deliberately excludes whitespace and every control
#: character, which is what makes log-line forgery impossible.
_HOST_SAFE_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    ".:-_%"
)


def _usable_host(value):
    """Return ``value`` as a usable bind address, or ``None``.

    Only a non-empty string is accepted.  Anything else -- ``None``, a blank or
    whitespace-only string, or a value of some other type -- means "not
    supplied", and the caller moves on to the next link of the chain.  Nothing
    here checks whether the address exists on this host: that is the kernel's
    answer to give at bind time, and :func:`_describe_bind_failure` turns its
    refusal into a readable message.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _usable_port(value):
    """Return ``value`` as an in-range TCP port number, or ``None``.

    ``0`` is accepted **here** and means "let the operating system assign an
    ephemeral port", which is how a test binds a listener without any risk of
    colliding with a server already running on 8000.  That is a bind-time
    argument, and it is the one place ``0`` is legal: a *configured* ``0`` is
    rejected by ``health.py``'s resolution at every tier, because an endpoint on a
    port the kernel picked cannot be reached by a probe configured in advance.

    ``bool`` is rejected explicitly, ahead of the ``int`` check it would
    otherwise satisfy as a subclass: ``True`` is a configuration mistake, not a
    request for port 1.  A decimal string is accepted so that a caller may pass
    a value straight through from an environment-shaped source, and a
    non-numeric or out-of-range value returns ``None`` rather than raising --
    a typo must degrade to the next link of the chain, never stop the endpoint
    from serving.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = int(value.strip(), 10)
        except ValueError:
            return None
    else:
        return None
    if 0 <= candidate <= 65535:
        return candidate
    return None


def _address_family(host):
    """Return the socket family to bind ``host`` with.

    :class:`~http.server.HTTPServer` is hard-wired to ``AF_INET``, so an IPv6
    literal in ``HEALTH_HOST`` -- ``::1``, or ``::`` for every interface --
    would otherwise be rejected by the kernel and surface as a puzzling
    "address not available".  A colon cannot appear in an IPv4 address or in a
    host name, so its presence is a reliable and cheap discriminator that needs
    no name resolution.
    """
    if isinstance(host, str) and ":" in host:
        return socket.AF_INET6
    return socket.AF_INET


def _sanitize_host(host):
    """Return ``host`` if it is safe to render, else :data:`UNSAFE_HOST_TEXT`.

    The host reaching a diagnostic is configuration-controlled: it arrives from
    ``HEALTH_HOST`` or from ``config/health.json``, both of which an operator (or
    anything able to set this process's environment) can fill with arbitrary
    text.  Writing it to a log unchecked makes the log a sink for that text, and
    a single newline in it is enough to forge a whole additional log entry --
    a fabricated "listening on ..." line, say, in a log a human or a parser
    later trusts.

    Rather than escape the dangerous characters and still print the value, an
    unusable host is replaced outright.  A host that is not a name, an IPv4
    literal or an IPv6 literal cannot be bound anyway, so there is nothing an
    operator could do with the exact bytes; the useful information is that the
    configured value was not a host at all, and that is what the placeholder
    says.  The permitted set covers every form that can legitimately appear,
    including an IPv6 scope such as ``fe80::1%eth0``.

    :param host: the configured host, of any type.
    :returns: the host unchanged, or :data:`UNSAFE_HOST_TEXT`.
    """
    if not isinstance(host, str):
        return UNSAFE_HOST_TEXT
    if not host or len(host) > _MAX_HOST_LENGTH:
        return UNSAFE_HOST_TEXT
    if any(character not in _HOST_SAFE_CHARACTERS for character in host):
        return UNSAFE_HOST_TEXT
    return host


def _bounded_configured_value(value):
    """Return *value* as text no longer than :data:`_MAX_CONFIGURED_LENGTH`.

    Used for the value quoted in a frozen-value warning, which is the one piece
    of document-supplied text this module renders in full rather than replacing.
    It is rendered because it is the actionable half of the message -- an
    operator needs to recognise the value in ``config/health.json`` -- and it is
    bounded because a document can declare a value of any length and the error
    stream should not carry it.  Control characters are removed downstream by
    :func:`_printable`, so a value cut here can still never forge a second line.

    :param value: the rejected value, of any type.
    :returns: the value as text, truncated with :data:`_TRUNCATION_MARK` when it
        exceeded the bound.
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= _MAX_CONFIGURED_LENGTH:
        return text
    return f"{text[:_MAX_CONFIGURED_LENGTH]}{_TRUNCATION_MARK}"


def _format_authority(host, port):
    """Return ``host:port`` in URL authority form, safely.

    An IPv6 literal is bracketed, as RFC 3986 requires, so that the address in
    the start-up line is one a reader can paste into a client unchanged.

    Sanitizing here rather than at each call site is deliberate: this is the one
    function through which every rendering of a host passes -- the start-up line
    and both lines of the bind-failure diagnostic -- so a future diagnostic
    cannot accidentally bypass the check.  The port is coerced through
    :func:`int` for the same reason, since a non-numeric port would otherwise be
    interpolated verbatim.
    """
    safe_host = _sanitize_host(host)
    try:
        safe_port = int(port)
    except (TypeError, ValueError):
        safe_port = UNSAFE_PORT_TEXT
    if ":" in safe_host:
        return f"[{safe_host}]:{safe_port}"
    return f"{safe_host}:{safe_port}"


def _printable(text):
    """Return ``text`` with control characters removed.

    The last line of defence for the diagnostic sink.  Every caller already
    passes text assembled from module constants, a sanitized host and a stable
    error category, so in practice this changes nothing -- which is the point: it
    holds even if a future diagnostic is added that forgets to sanitize its
    inputs.  Removing rather than escaping keeps one logical line on one physical
    line, which is what makes the output greppable.
    """
    if not isinstance(text, str):
        return str(text).replace("\n", " ").replace("\r", " ")
    return "".join(
        character if character.isprintable() or character == " " else " "
        for character in text
    )


def _write_stderr(lines):
    """Write diagnostic ``lines`` to standard error, flushed, never raising.

    Diagnostics are a courtesy to whoever is reading the log; they are not the
    mission.  If standard error has been closed or the pipe on the far end has
    gone away, ``OSError`` (including ``BrokenPipeError``) is swallowed so that
    reporting a problem can never itself become the problem -- most importantly,
    so that it can never mask the exit status the caller is about to return.
    ``ValueError`` is caught alongside it for the detached-stream case, where the
    underlying buffer is gone rather than merely broken.

    Each line is passed through :func:`_printable` first, so one call can never
    produce more log entries than it was given lines.
    """
    for line in lines:
        try:
            print(f"{_PROGRAM}: {_printable(line)}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            return


# The listener


class HealthHTTPServer(ThreadingHTTPServer):
    """The tier's listener: threaded, *bounded*, promptly closeable, single-port.

    Threaded rather than :class:`~http.server.HTTPServer` because a health
    endpoint must answer while several clients poll it at once: with one thread, a
    client that connects and stalls holds the accept loop and every later probe
    queues behind it, so a liveness probe a slow client can silence reports the
    opposite of the truth.

    Threaded, but **not unboundedly threaded**.  ``ThreadingMixIn`` spawns one
    thread per accepted connection and imposes no ceiling, so the plain mixin
    answers "how many clients at once?" with "as many as ask" -- the wrong answer
    for a resource whose whole job is to keep answering.  Each connection costs a
    thread, a stack and a descriptor for as long as it is held, and because the
    handler speaks HTTP/1.1 a client that says nothing at all still holds one for
    the full ten-second idle timeout, so a caller can turn stalled peers into
    unbounded threads and eventually meet ``RuntimeError: can't start new
    thread`` -- at which point the accept loop stops serving *everyone*.

    So :meth:`process_request` admits at most :attr:`max_concurrent_requests`
    connections at a time and applies explicit back-pressure beyond that:
    :attr:`admission_wait_seconds` of waiting, then an immediate refusal -- closed
    with no response written, and counted.  Three properties follow, and each is
    the reason for the shape.  *Thread count is a property of this class, not of
    the caller*, because the ceiling is a fixed number this class owns.  *Overload
    is bounded and honest*, because the newest arrival is closed at once, which a
    client reads as a reset it can retry rather than a timeout it must wait out;
    running the overflow on the caller's thread is deliberately rejected here,
    since that caller *is* the accept loop.  *Shutdown stays as prompt as it was*,
    because nothing in the stop path waits on an in-flight reply -- see
    ``block_on_close`` below, whose contract this preserves exactly.  The Java
    tier of this composition makes the same choice with a fixed pool over a
    bounded queue and an explicit rejection policy; the mechanism differs because
    the runtimes do, the guarantee does not.

    All five inherited ``socketserver`` class attributes are set explicitly
    because each is load-bearing:

    ``daemon_threads = True``
        Request threads must never keep the interpreter alive after the main
        thread finishes.  The base class already sets it; repeated here so the
        guarantee is visible where it is depended on.

    ``block_on_close = False``
        Keeps shutdown prompt.  With ``True``, ``server_close`` joins in-flight
        request threads, so one keep-alive client could stall it for the handler's
        full ten-second idle timeout -- long enough for a supervisor to read as a
        hang and ``SIGKILL``.  Little is lost: the only interruptible work is a
        reply to a caller already told the process is going away.

    ``allow_reuse_address = True``
        ``SO_REUSEADDR``, so an immediate restart is not blocked by the previous
        socket draining ``TIME_WAIT``.  It does *not* permit binding a port
        another process is actively listening on, which keeps the port-in-use
        diagnostic honest.

    ``allow_reuse_port = False``
        Must never be relaxed: ``SO_REUSEPORT`` would let a second instance bind
        the *same live* port and take a share of the connections, so a duplicate
        start would look successful while probes reached an arbitrary instance.

    ``request_queue_size = socket.SOMAXCONN``
        The ``listen()`` backlog.  ``socketserver`` defaults it to 5, too small
        for a resource whose purpose is to be polled: on overflow the kernel drops
        the handshake and the client waits out a retransmit, so a probe can time
        out against a server answering everything it actually receives.
        ``SOMAXCONN`` defers to the operating system's ceiling instead of guessing.

        The accept queue and the admission bound are complementary rather than
        alternatives, and conflating them is the mistake to avoid: the queue
        decides how many completed handshakes the *kernel* will hold for this
        listener, while the bound decides how much work this *process* will carry
        at once.  A deep queue with unbounded admission converts a burst into
        unbounded threads faster; a shallow queue with a bound drops handshakes
        the process could have served.  Both are therefore set.

    The ceiling, and the wait allowed before refusing, are class attributes for
    the same reason the five above are: they are the bound, so they belong where a
    reader looks for it, and a subclass in a test can lower the ceiling to reach
    the refusal path without opening sixty-five sockets.
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    allow_reuse_port = False
    request_queue_size = socket.SOMAXCONN

    #: Ceiling on concurrently served connections.  See
    #: :data:`MAX_CONCURRENT_REQUESTS` for how the value is derived, and
    #: :meth:`process_request` for what happens at the ceiling.  Read once per
    #: instance, in :meth:`__init__`, so a subclass may lower it for a test or
    #: raise it for a deployment without touching this module.
    max_concurrent_requests = MAX_CONCURRENT_REQUESTS

    #: Seconds the accept loop waits for a permit before refusing.  See
    #: :data:`ADMISSION_WAIT_SECONDS`.
    admission_wait_seconds = ADMISSION_WAIT_SECONDS

    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        bind_and_activate=True,
    ):
        """Bind ``server_address``, selecting the family it actually needs.

        The socket family must be decided before the base class creates the
        socket, and the base class reads it from ``self``, so setting it here
        before delegating is the supported way to bind an IPv6 address -- and it
        keeps the choice per-instance rather than mutating a class attribute a
        concurrently constructed server would see.

        The capitalised parameter name follows :mod:`socketserver` exactly, so
        this subclass stays a drop-in replacement for keyword callers.

        The admission permits and the refusal counter are created here, before
        the base class binds anything, so no accepted connection can reach
        :meth:`process_request` without them.  A
        :class:`threading.BoundedSemaphore` is used rather than a plain one
        deliberately: a release not matched by an acquire would be a defect in
        this class, and the bounded variant turns it into an immediate
        ``ValueError`` instead of a ceiling that silently grows.
        """
        self.address_family = _address_family(server_address[0])
        self._admission = threading.BoundedSemaphore(self.max_concurrent_requests)
        self._refusal_lock = threading.Lock()
        self._refused_requests = 0
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    # Bounded admission.  ``socketserver`` calls ``process_request`` once per
    # accepted connection, from the accept loop, and treats whatever it does as
    # the dispatch policy.  Overriding it replaces that policy while leaving the
    # per-connection lifecycle (serve, finish, error, close) exactly as
    # ``socketserver`` defines it.

    def refused_request_count(self):
        """Return how many connections have been refused for want of a permit.

        Saturation is *counted* rather than logged, and the choice is deliberate.
        The contract admits no per-request logging, and a refusal arrives exactly
        when the process is already under pressure -- the moment a per-connection
        log line is least affordable.  The Java tier's back-pressure policy is
        silent for the same reason.  A counter keeps the event observable without
        making load noisy: a test can assert it, and an operator can read it from
        an interactive session.

        :returns: the count, read under the lock that guards it, so a caller never
            observes a partially updated value.
        """
        with self._refusal_lock:
            return self._refused_requests

    def process_request(self, request, client_address):
        """Admit one connection, or refuse it explicitly and close it.

        This is the whole of the admission decision, and it runs on the accept
        loop's own thread -- which is what makes the wait below back-pressure
        rather than bookkeeping: while it waits, this listener accepts nothing
        further and the kernel holds the pending handshakes in the accept queue
        instead.

        Three outcomes, all defined:

        * **A permit is available** -- the connection is handed to the base class,
          which serves it on its own thread exactly as before.  The permit is
          released by :meth:`process_request_thread` when that thread finishes.
        * **No permit within** :attr:`admission_wait_seconds` -- the connection is
          refused: closed immediately, with **no response written**, and counted.
          Nothing is invented on the wire, because the contract's status vocabulary
          is exactly ``200``, ``404`` and ``405``, and a saturated listener has
          none of those to say; a closed connection is unambiguous where an
          undocumented status code would not be.  The close goes through
          :meth:`~socketserver.TCPServer.shutdown_request`, the same path a served
          connection ends on, so the peer reads end-of-stream at once instead of
          waiting out a timeout, and no descriptor is leaked.  Serving the
          connection inline on the accept thread -- the Java tier's caller-runs
          policy -- was considered and rejected *at this tier*, and the reason is a
          difference in the runtimes rather than a difference of opinion.  That
          tier's dispatcher has already read the whole request before its executor
          is involved, so running the rejected work inline costs microseconds of
          payload building.  Here the accept loop hands over a *raw socket*:
          running it inline would mean conducting the entire HTTP conversation on
          the accept thread, including waiting up to the handler's ten-second idle
          timeout for a client that may never speak.  One stalled client would then
          stop the listener accepting at all -- precisely the failure the bound
          exists to prevent.
        * **The thread cannot be started** -- the permit is returned before the
          exception propagates, so a transient failure cannot erode the ceiling one
          permit at a time.  ``socketserver`` reports and closes such a request
          itself.

        :param request: the accepted connection socket.
        :param client_address: the peer address, as :mod:`socketserver` supplies
            it, passed through untouched and deliberately never rendered into a
            diagnostic.
        """
        if not self._admission.acquire(timeout=self.admission_wait_seconds):
            with self._refusal_lock:
                self._refused_requests += 1
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._admission.release()
            raise

    def process_request_thread(self, request, client_address):
        """Serve one admitted connection, then return its permit.

        The base implementation is called unchanged and the release sits in a
        ``finally``, so the permit comes back on every path a connection can end
        on -- a completed exchange, a peer that disappeared, an expired idle
        timeout, or a handler error the base class routes to ``handle_error`` --
        and the ceiling can never erode.

        :param request: the accepted connection socket.
        :param client_address: the peer address, as :mod:`socketserver` supplies it.
        """
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._admission.release()


# Address resolution


def resolve_bind_address(host=None, port=None):
    """Return the ``(host, port)`` this process should bind.

    This function **validates**; it does not resolve.  The precedence chain
    ``environment variable -> configuration file -> compiled-in literal`` is
    applied exactly once in this tier, by ``health.py`` at import time, and its
    result is read from :data:`health.HOST` and :data:`health.PORT`.  Repeating
    the chain here would create a second place for the defaults to live and a
    second place for them to drift.

    What is applied here is the *ordering* an entry point still needs:

    1. an explicit argument, which is how a test asks for port ``0`` and the
       loopback interface without touching the process environment;
    2. the value ``health.py`` resolved;
    3. :data:`DEFAULT_HOST` / :data:`DEFAULT_PORT`, the compiled-in literals.

    Step 3 looks redundant -- ``health.py`` guarantees usable values -- and is
    kept deliberately.  The literal fallback is a contract requirement rather
    than a nicety: the endpoint must still serve when its configuration is
    absent from a deployment, which is exactly the failure a health endpoint has
    to survive.  Belonging to the entry point as well as to the configuration
    reader means no single edit can remove it.

    Never raises, and never returns a value the caller has to re-check: the host
    is a non-empty ``str`` and the port an ``int`` in ``0..65535``.
    """
    resolved_host = _usable_host(host)
    if resolved_host is None:
        resolved_host = _usable_host(health.HOST)
    if resolved_host is None:
        resolved_host = DEFAULT_HOST

    resolved_port = _usable_port(port)
    if resolved_port is None:
        resolved_port = _usable_port(health.PORT)
    if resolved_port is None:
        resolved_port = DEFAULT_PORT

    return resolved_host, resolved_port


def create_server(
    host=None,
    port=None,
    handler_class=health.HealthRequestHandler,
    server_class=HealthHTTPServer,
):
    """Build and bind a listener for the health endpoint, without serving.

    Binding and serving are separated so a caller can learn the port it actually
    got -- which matters when it asked for ``0`` -- and arrange its own teardown
    before a single request is accepted.  That is what makes the endpoint
    testable end to end without a fixed port, and why this returns rather than
    blocking.  Call :func:`serve_until_signalled` to run the result, or
    ``server_close()`` if you decide not to.

    Raises ``OSError`` when the address cannot be bound, typically
    ``EADDRINUSE``.  The base class closes the partially built socket before the
    exception leaves, so a failed call leaks no descriptor;
    :func:`_describe_bind_failure` turns it into a readable diagnostic.
    """
    resolved_host, resolved_port = resolve_bind_address(host, port)
    return server_class((resolved_host, resolved_port), handler_class)


def startup_line(host, port, path=None):
    """Return the single line this process logs at start-up.

    The line names the address the listener is actually bound to and the
    application serving it, in one flushed write, and it is the only thing this
    process ever writes to standard output.

    ``0.0.0.0`` appears here as what it is -- the bind address, meaning every
    local interface -- and is not rewritten to something dialable, because the
    log records what the process did rather than what a client should type.
    Probes target ``127.0.0.1``.

    ``host`` and ``port`` are the values the socket reports, so an ephemeral port
    is logged as the number the kernel chose rather than as ``0``.  The result
    carries no trailing newline and reads
    ``listening on http://0.0.0.0:8000/health (child_repo_10_LOC 1.0.0)``.
    """
    served_path = path if path is not None else health.HEALTH_PATH
    authority = _format_authority(host, port)
    return (
        f"listening on http://{authority}{served_path}"
        f" ({health.APP_NAME} {health.APP_VERSION})"
    )



# Serving and orderly shutdown


def serve_until_signalled(server, signals=SHUTDOWN_SIGNALS):
    """Serve requests until a shutdown signal arrives, then close cleanly.

    Returns only when the listener has stopped accepting and its socket has been
    released, so a caller may immediately rebind the same port.

    The deadlock this avoids: :meth:`~socketserver.BaseServer.shutdown` stops the
    :meth:`~socketserver.BaseServer.serve_forever` loop and then *waits* for
    that loop to acknowledge.  CPython runs signal handlers on the main
    thread -- the very thread inside ``serve_forever`` -- so a handler that
    calls ``shutdown()`` directly waits for a loop it is itself blocking.  The
    process hangs, a supervisor escalates to ``SIGKILL``, and the "orderly"
    shutdown was never orderly.

    The fix is to move the ``shutdown()`` call off that thread, and to do so
    without creating a thread from inside a signal handler -- starting a thread
    takes locks, and taking a lock in a handler that may have interrupted the
    main thread while it held one is its own deadlock.  So the helper thread is
    started *before* serving begins and parked on an
    :class:`~threading.Event`.  The handler's whole job is ``event.set()``:
    one lock, held by nobody else on the main thread, so it can always be taken.
    The parked thread wakes and calls ``shutdown()`` from a thread that is not
    the loop's own, which is precisely what the method requires.

    Three further paths are covered, because "exits 0 without a traceback" has
    to hold however the stop arrives:

    * ``KeyboardInterrupt`` -- Ctrl-C delivered before the handlers are
      installed, or while Python's default ``SIGINT`` behaviour is still active,
      raises rather than setting the event.  It is caught and treated as the
      shutdown request it is.
    * ``signal.signal`` failing -- it raises :exc:`ValueError` off the main
      thread (a test may drive this function from a worker) and :exc:`OSError`
      where a platform does not support a signal.  Neither is fatal: serving
      proceeds without signal-driven shutdown, and the caller stops the server
      by calling ``shutdown()`` itself.
    * the ``finally`` block -- runs on every path, including an unexpected
      exception, so the socket is released even when the exit is not graceful.

    Nothing is written to either stream on the ordinary path.  The start-up line
    is this process's only output, and a "shutting down" notice would make the log
    two lines where the contract says one.  The single exception is a shutdown
    that did not complete: that is not the expected path, it cannot be observed
    from anywhere else, and it changes what a supervisor should do next, so it
    gets one line and a non-zero status.

    :param server: a bound server, as returned by :func:`create_server`.
    :param signals: the signals to treat as a shutdown request.  Defaults to
        :data:`SHUTDOWN_SIGNALS` (``SIGTERM`` and ``SIGINT``).
    :returns: :data:`EXIT_OK` when the shutdown completed -- a shutdown that was
        asked for is a success -- or :data:`EXIT_SHUTDOWN_FAILURE` when
        ``shutdown()`` raised or failed to return within
        :data:`_SHUTDOWN_JOIN_TIMEOUT`.
    """
    stop_requested = threading.Event()

    # The two parameters are the signal-handler signature the standard library
    # calls this with; neither is needed, and neither may be dropped.
    def _request_shutdown(signum, frame):
        """Signal handler: record the request and return immediately.

        Deliberately the smallest possible body.  Setting the event is the only
        action taken; every consequence happens on the helper thread below,
        which is safe to block.
        """
        stop_requested.set()

    # Reasons the shutdown did not complete cleanly, appended by the helper
    # thread and read by the main thread after the join below.  A list is used
    # because ``append`` is atomic and the join establishes the ordering, so no
    # additional lock is needed for the hand-off.
    shutdown_failures = []

    def _drive_shutdown():
        """Park until a shutdown is requested, then stop the accept loop."""
        stop_requested.wait()
        try:
            server.shutdown()
        except Exception as error:
            # ``shutdown`` sets a flag and waits on an event, so there is no
            # ordinary failure mode.  The guard exists because this is a daemon
            # thread: an escaping exception would print a traceback that looked
            # like an application fault, and the ``finally`` block below closes
            # the socket regardless.
            #
            # What it must not do is stay quiet and let the process still report
            # success.  A shutdown that failed means the accept loop may still be
            # running and the port may still be held, so a supervisor that reads
            # exit 0 and immediately restarts would hit a confusing bind failure
            # instead of the real cause.  The category is recorded here and turned
            # into both a diagnostic and a non-zero exit status below.
            shutdown_failures.append(_error_category(error))

    previous_handlers = {}
    for signal_number in signals:
        try:
            previous_handlers[signal_number] = signal.signal(
                signal_number, _request_shutdown
            )
        except (OSError, ValueError):
            # Not the main thread, or a signal this platform does not support.
            # Serving is unaffected; only signal-driven shutdown is unavailable.
            continue

    shutdown_thread = threading.Thread(
        target=_drive_shutdown,
        name="health-shutdown",
        daemon=True,
    )
    shutdown_thread.start()

    try:
        server.serve_forever(poll_interval=_POLL_INTERVAL)
    except KeyboardInterrupt:
        # Ctrl-C outside the installed handler: the same request, arriving as an
        # exception.  Handled here so it never reaches the interpreter's default
        # traceback.
        pass
    finally:
        # Release the parked thread whichever way the loop ended -- on a signal
        # it is already running, and on any other exit it would otherwise stay
        # parked for the life of the process.
        stop_requested.set()
        shutdown_thread.join(_SHUTDOWN_JOIN_TIMEOUT)
        if shutdown_thread.is_alive():
            # The join expired, so ``shutdown()`` itself has not returned. The
            # thread is a daemon and cannot keep the interpreter alive, but the
            # accept loop was never confirmed stopped -- which is a failed
            # shutdown by any useful definition, and is recorded as one.
            shutdown_failures.append("Timeout")
        # Close after ``shutdown`` has been driven, in that order: the loop
        # stops accepting first, then the listening socket is released.  With
        # ``block_on_close`` False this returns at once, so the port is free for
        # an immediate rebind.
        server.server_close()
        # Leave the process's signal disposition as it was found, so that
        # calling this function in-process -- from a test, say -- has no lasting
        # effect on the interpreter.
        for signal_number, handler in previous_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (OSError, ValueError):
                continue

    if shutdown_failures:
        # One line, built from a stable category, reported after the signal
        # dispositions have been restored so the report cannot itself be
        # interrupted by a second signal mid-write.
        _write_stderr(
            [
                "shutdown did not complete cleanly"
                f" ({', '.join(shutdown_failures)});"
                " the listening socket was closed regardless",
            ]
        )
        return EXIT_SHUTDOWN_FAILURE

    return EXIT_OK


# Bind failure reporting


def _error_category(error):
    """Return a stable, log-safe category for ``error``.

    Prefers the symbolic ``errno`` name -- ``EADDRINUSE``, ``EAFNOSUPPORT`` --
    because it is the same token on every host and in every locale, which makes
    it something an operator can search for and a runbook can name.  Falls back
    to the exception's class name when there is no usable errno, as for
    :exc:`socket.gaierror` variants that carry a resolver code instead.

    Neither source can contain a path, an address or a control character: an
    errno name and a Python class name are both identifiers.

    :param error: the exception to categorize.
    :returns: a short identifier-shaped token, never empty.
    """
    number = getattr(error, "errno", None)
    if isinstance(number, int):
        symbolic = errno.errorcode.get(number)
        if symbolic:
            return symbolic
        return f"errno{number}"
    return type(error).__name__ or "OSError"


def _describe_bind_failure(port, error):
    """Return ``(reason, remedy)`` describing why a bind failed.

    A failure to bind is an ordinary operational condition -- the port is
    taken, the address is not local, the port is privileged -- and not a defect
    in this program, so it is reported as a sentence a reader can act on rather
    than as a traceback.  Every branch names the override that fixes it, because
    a diagnostic that does not say what to do next is only half a diagnostic.

    The host is deliberately not a parameter: no branch below varies by it, and
    :func:`_report_bind_failure` already names the full ``host:port`` authority
    on the first line of the diagnostic.  ``port`` *is* needed, because the
    privileged-port case is distinguishable only by its value.  The result is a
    two-item tuple of short, complete sentences.
    """
    number = getattr(error, "errno", None)

    if number == errno.EADDRINUSE:
        return (
            "address already in use",
            "another process is listening there -- possibly an earlier"
            f" {_PROGRAM}. Stop it, or pick a free port with"
            f" {health.ENV_PORT}, e.g. {health.ENV_PORT}=8100 python"
            f" {_PROGRAM}",
        )

    if number in (errno.EACCES, errno.EPERM):
        if port < 1024:
            return (
                "permission denied",
                f"port {port} is privileged and needs elevated rights this"
                " process should not have. Use an unprivileged port with"
                f" {health.ENV_PORT}, e.g. {health.ENV_PORT}={DEFAULT_PORT}"
                f" python {_PROGRAM}",
            )
        return (
            "permission denied",
            "the operating system refused the bind. Check any sandbox or"
            f" security policy, or choose another port with {health.ENV_PORT}",
        )

    if number == errno.EADDRNOTAVAIL:
        return (
            "address not available on this host",
            f"{health.ENV_HOST} must name an address assigned to a local"
            f" interface. {DEFAULT_HOST} binds every interface and is the"
            " default",
        )

    if isinstance(error, socket.gaierror):
        return (
            "host could not be resolved",
            f"{health.ENV_HOST} must be a resolvable name or an IP literal."
            f" {DEFAULT_HOST} binds every interface and is the default",
        )

    # Anything else: report a stable category rather than the operating system's
    # own wording.  ``strerror`` and ``str(error)`` are locale-dependent and, for
    # some errors, interpolate the address or filename that failed -- so they
    # make the diagnostic both unstable across hosts and capable of echoing
    # configuration back into the log.  The symbolic errno name is the useful,
    # greppable part and is identical everywhere; where there is no errno to name,
    # the exception's own class name serves the same purpose.
    return (
        f"bind failed ({_error_category(error)})",
        f"adjust {health.ENV_HOST} or {health.ENV_PORT} and retry",
    )


def _report_bind_failure(host, port, error):
    """Write the two-line bind diagnostic to standard error.

    Two lines, not a traceback: the condition is expected, and a stack trace
    would bury the one fact that matters -- which address could not be taken --
    under frames from the standard library.
    """
    reason, remedy = _describe_bind_failure(port, error)
    _write_stderr(
        [
            f"cannot bind {_format_authority(host, port)}: {reason}",
            remedy,
        ]
    )


def report_frozen_value_conflicts():
    """Report, once at start-up, every configured value rejected as frozen.

    The resource path and the ``status`` literal are read from
    ``config/health.json`` but validated against the contract before they are
    adopted, so ``health.py`` serves the contract's literal whatever that document
    says.  Serving the right thing is not by itself enough: a deployment that
    edited the document expecting an effect would otherwise get silence, and
    would discover the truth only from a monitoring gap.  One line per rejected
    value names the key, what was configured, and what is served instead.

    Standard error, because this reports a misconfiguration rather than normal
    progress -- and emitted here rather than in ``health.py`` because that module
    must stay importable without writing to either stream.  A correctly configured
    deployment prints nothing at all, so this can add no noise to an ordinary
    start-up.

    The rejected value passes through :func:`_bounded_configured_value` first, so
    a document declaring an enormous value cannot put its whole length on the
    error stream, and through :func:`_printable` in :func:`_write_stderr`, so it
    cannot forge a second log line.  The line count is bounded by construction:
    there are two frozen keys, so there can never be more than two of these.

    :returns: the number of conflicts reported, so a caller (or a test) can
        assert on the count without parsing standard error.
    """
    conflicts = health.frozen_value_conflicts()
    document = f"{health.CONFIG_PATH.parent.name}/{health.CONFIG_PATH.name}"
    _write_stderr(
        [
            f'ignoring configured {key} '
            f'"{_bounded_configured_value(configured)}": {key} is frozen at '
            f'"{frozen}" by the /health contract and is not a deployment '
            f'setting. Remove the value or restore it to "{frozen}" in '
            f"{document}."
            for key, configured, frozen in conflicts
        ]
    )
    return len(conflicts)


# ---------------------------------------------------------------------------
# Entry point


def main():
    """Bind, announce, serve, and return this process's exit status.

    The whole of the entry point, in the order it happens:

    1. report a degraded configuration, if there is one, in one sanitized line;
    2. report any configured value rejected for redefining a frozen contract
       constant (:func:`report_frozen_value_conflicts`) -- nothing at all for a
       correctly configured deployment;
    3. resolve the address to bind (:func:`resolve_bind_address`);
    4. bind it, or report why not and return :data:`EXIT_BIND_FAILURE`;
    5. announce the bound address in exactly one flushed line;
    6. serve until ``SIGTERM`` or ``SIGINT``, then close and return
       :data:`EXIT_OK`.

    Step 1 is what makes the fallback chain observable. Falling back to a literal
    keeps the endpoint answering when its configuration is missing, which is
    correct; doing so invisibly is not, because the endpoint then reports ``UP``
    with fallback-sourced identity and nothing anywhere says so.  Both reports
    come before the bind so that either warning is visible even when the bind
    then fails: a misconfigured document and an occupied port are independent
    problems, and one must not hide the other.

    Step 4 never falls back to a different port.  A probe in this composition
    always targets a known port -- 8000 here -- so a silent move would replace a
    clear failure with a confusing one: a process that appeared to start while
    every probe against it timed out.

    There is no command-line parsing, by design.  Configuration is the
    environment and ``config/health.json``, resolved by ``health.py``; adding
    flags would create a third source of truth for values that already have
    exactly one.  ``python server.py`` takes no arguments and needs none.

    :returns: :data:`EXIT_OK` after an orderly shutdown,
        :data:`EXIT_BIND_FAILURE` if the listener could not be bound, or
        :data:`EXIT_SHUTDOWN_FAILURE` if it served but could not be stopped
        cleanly.
    """
    # Report a degraded configuration before anything else, so the reason the
    # values below look wrong is already in the log when they are used. This is
    # the one place it is reported: ``health.py`` deliberately stays silent on
    # import, because a module that logs when it is merely read corrupts the
    # output of every program that reads it.
    #
    # Nothing is printed on the ordinary path. A "configuration OK" line on every
    # run would train a reader to skip the line, which is exactly the run where
    # it would have mattered.
    degraded = health.describe_configuration_degradation()
    if degraded is not None:
        _write_stderr([degraded])

    # Then report anything the deployment declared that this tier refused to
    # adopt because it was not the contract's own literal.  A correctly
    # configured deployment prints nothing here either.
    report_frozen_value_conflicts()

    host, port = resolve_bind_address()

    try:
        server = create_server(host, port)
    except OSError as error:
        _report_bind_failure(host, port, error)
        return EXIT_BIND_FAILURE

    # Announce the address the socket reports rather than the one requested, so
    # an ephemeral port is logged as the number the kernel actually assigned.
    bound_host, bound_port = server.server_address[:2]
    try:
        print(startup_line(bound_host, bound_port), flush=True)
    except OSError:
        # Standard output is closed or its pipe has gone away. The listener is
        # bound and healthy; losing the announcement is not a reason to refuse
        # to serve, and the endpoint itself is how a caller confirms liveness.
        pass

    return serve_until_signalled(server)


if __name__ == "__main__":
    try:
        _exit_status = main()
    except KeyboardInterrupt:
        # Ctrl-C in the narrow window before the handlers are installed -- while
        # binding, or while announcing. Still an orderly stop, still exit 0, and
        # still no traceback.
        _exit_status = EXIT_OK
    sys.exit(_exit_status)
