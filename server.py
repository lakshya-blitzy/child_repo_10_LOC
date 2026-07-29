"""Level 2 (``child_repo_10_LOC``) health endpoint entry point.

This module is the Python tier's long-lived process: it binds a TCP listener,
serves the ``/health`` contract through the request handler defined in the
sibling ``health.py``, and shuts that listener down in an orderly fashion when
the process is asked to stop.  It does nothing else.  Every decision about
*what* the endpoint answers -- the payload, the compact serialization, the
header set, the ``405`` and the ``404`` -- belongs to ``health.py`` and is
deliberately not restated here, because duplicating a contract across two files
is how three independently written tiers drift out of agreement.

Why this file exists at all
---------------------------

This tier has **two parallel entry points**, and that is the whole reason for
the split:

===========================  ==========================================
``python app.py``            prints ``Hello Lakshya`` and exits 0.
                             Pre-existing behaviour, asserted by CI as a
                             permanent regression gate.  Untouched.
``python server.py``         binds ``0.0.0.0:8000``, serves ``/health``,
                             stays alive.  This file.
===========================  ==========================================

The health endpoint is purely additive.  Before it, nothing in this composition
was long-lived: all behaviour happened during script initialisation, with no
event loop, no asynchrony and no deferred callback.  A listener makes a process
long-lived for the first time, so the listener lives *here* -- in a separate
entry point -- and never on the default path.  ``python app.py`` therefore still
terminates immediately, which is exactly what the preservation requirement
demands.

What this module guarantees
---------------------------

* **Nothing binds at import.**  Importing this module defines classes and
  functions and returns; it opens no socket, starts no thread, installs no
  signal handler and writes nothing to either stream.  ``python -c "import
  server"`` is inert, which is what lets a test import the factory below and
  bind its own listener on an ephemeral port.
* **One line of output, once.**  The bound address is announced exactly once,
  at start-up, flushed immediately so it appears even when standard output is a
  pipe (which is how CI captures it).  Nothing is logged per request -- the
  handler in ``health.py`` silences that -- and nothing is logged on shutdown
  either, so the log holds exactly one line for the entire lifetime of the
  process.  There is no logging framework anywhere in this tier.
* **Prompt, orderly shutdown on ``SIGTERM`` and ``SIGINT``.**  Both stop the
  accept loop, release the listening socket and exit ``0`` with no traceback.  A
  container stop is therefore not a hard kill, and a workflow teardown leaves no
  orphaned port binding to break the next run.  See
  :func:`serve_until_signalled` for how the deadlock inherent in the naive
  approach is avoided.
* **A failed bind is reported, never disguised.**  If the port is already in
  use the process explains which address it could not take and how to choose
  another, then exits non-zero.  It never silently moves to a different port:
  every probe in this composition targets a known port, and a silent shift would
  turn a clear failure into a confusing one.
* **Zero third-party packages.**  ``http.server.ThreadingHTTPServer`` from the
  standard library, and nothing else.  Flask, FastAPI, Starlette, uvicorn,
  gunicorn and waitress were each considered and rejected: any one of them would
  give this repository its first pip dependency and its first
  virtual-environment contract.  The composition's third-party runtime
  dependency count is zero and stays zero.

Configuration
-------------

The bound address resolves through the uniform precedence chain
``environment variable -> configuration file -> compiled-in literal``, applied
in exactly one place in this tier: ``health.py``, at import time.  This module
*consumes* the result (:data:`health.HOST`, :data:`health.PORT`) rather than
re-deriving it, so the two files can never disagree about a default.  For this
tier the overrides are ``HEALTH_HOST`` and ``HEALTH_PORT``; the bare ``HOST``
and ``PORT`` belong to the Level 1 apex application and are deliberately not
read here.

===================  ==========================================
``HEALTH_HOST``      bind address; default ``0.0.0.0``
``HEALTH_PORT``      bind port; default ``8000``
===================  ==========================================

``0.0.0.0`` is a bind address only -- it means "every local interface" and is
never a destination.  Probe the running server on ``127.0.0.1``.

Usage
-----

.. code-block:: bash

    python server.py
    curl -i http://127.0.0.1:8000/health
    HEALTH_PORT=8100 HEALTH_HOST=127.0.0.1 python server.py

Level independence
------------------

Nothing here references another tier.  The Level 1 JavaScript application and
the Level 3 Java application implement the same contract in their own
languages, from their own repositories, and the three share a *documented*
contract (``docs/health-endpoint.md`` in the apex repository) rather than a
runtime artefact.  There is no import across a tier boundary, in either
direction, and no file in this repository reads that document at run time.
"""

import errno
import signal
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Tier-local import bootstrap
#
# ``health`` sits beside this file at the repository root.  Launching a script
# by path already puts that directory on ``sys.path``, but two other entry
# routes do not: ``python -m server`` resolves against the current working
# directory, and an embedded interpreter may start with a path that omits it
# entirely.  Deriving the directory from ``__file__`` rather than from the
# working directory makes every route behave identically -- from the repository
# root, from a parent directory, or as ``/app/server.py`` inside the container
# image, which is precisely the CWD independence the container entry point
# depends on.
#
# The insert is idempotent and additive: an already-present entry is left where
# it is, so a caller that has deliberately arranged its own path ordering keeps
# it.
# ---------------------------------------------------------------------------

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

import health  # noqa: E402 - deliberately after the sys.path bootstrap above

__all__ = [
    # The listener.
    "HealthHTTPServer",
    # Declared defaults and process-level constants.
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "SHUTDOWN_SIGNALS",
    "EXIT_OK",
    "EXIT_BIND_FAILURE",
    # Behaviour, in the order the entry point uses it.
    "resolve_bind_address",
    "create_server",
    "startup_line",
    "serve_until_signalled",
    "main",
]

# ---------------------------------------------------------------------------
# Declared defaults
#
# Re-exported from ``health`` rather than spelled again, so that "the default
# port is 8000" is stated in exactly one place in this tier.  A consumer -- the
# test suite, or a reader -- can assert against these names instead of
# hard-coding a literal that would then need changing in two files.
# ---------------------------------------------------------------------------

#: Bind address of last resort: every local interface.
DEFAULT_HOST = health.FALLBACK_HOST

#: Bind port of last resort.  Each tier owns a distinct default port (3000 for
#: Level 1, 8000 here, 8080 for Level 3) because all three applications may run
#: simultaneously on one host during validation -- and in the apex workflow they
#: do.  Reusing another tier's port would make them collide.
DEFAULT_PORT = health.FALLBACK_PORT

#: The signals that mean "stop serving".  ``SIGTERM`` is what a container
#: runtime and a CI teardown send; ``SIGINT`` is Ctrl-C at a terminal.  Both are
#: handled identically, and both exit ``0``: an orderly stop that was asked for
#: is a success, not a failure.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

#: Exit status after an orderly shutdown.
EXIT_OK = 0

#: Exit status when the listener could not be bound.  Deliberately non-zero and
#: deliberately *not* accompanied by a fallback to some other port: a probe that
#: targets 8000 must fail loudly if 8000 could not be taken.
EXIT_BIND_FAILURE = 1

#: How long :meth:`~socketserver.BaseServer.serve_forever` waits between checks
#: of its shutdown flag.  A signal interrupts the wait immediately (PEP 475
#: retries the call with the *remaining* timeout), so this value bounds how long
#: the loop can take to notice a shutdown request -- a fifth of a second, which
#: keeps a container stop and a CI teardown prompt while costing five idle
#: wake-ups a second.
_POLL_INTERVAL = 0.2

#: Upper bound on the wait for the shutdown helper thread to finish.  It exists
#: only so that a pathological state can never turn "stopping" into "hanging";
#: in practice the join returns in microseconds.  The thread is a daemon, so
#: even an expired join cannot keep the interpreter alive.
_SHUTDOWN_JOIN_TIMEOUT = 5.0

#: Prefix for diagnostics on standard error, so a message in an interleaved CI
#: log is attributable.  Derived from this file's own name rather than from
#: ``sys.argv[0]``, which an embedding process controls and may leave empty.
_PROGRAM = Path(__file__).name


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

    ``0`` is accepted and means "let the operating system assign an ephemeral
    port", which is how a test binds a listener without any risk of colliding
    with a server already running on 8000.

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


def _format_authority(host, port):
    """Return ``host:port`` in URL authority form.

    An IPv6 literal is bracketed, as RFC 3986 requires, so that the address in
    the start-up line is one a reader can paste into a client unchanged.
    """
    if isinstance(host, str) and ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _write_stderr(lines):
    """Write diagnostic ``lines`` to standard error, flushed, never raising.

    Diagnostics are a courtesy to whoever is reading the log; they are not the
    mission.  If standard error has been closed or the pipe on the far end has
    gone away, ``OSError`` (including ``BrokenPipeError``) is swallowed so that
    reporting a problem can never itself become the problem -- most importantly,
    so that it can never mask the exit status the caller is about to return.
    """
    for line in lines:
        try:
            print(f"{_PROGRAM}: {line}", file=sys.stderr, flush=True)
        except OSError:
            return


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


class HealthHTTPServer(ThreadingHTTPServer):
    """The tier's listener: threaded, promptly closeable, single-port.

    :class:`~http.server.ThreadingHTTPServer` rather than the single-threaded
    :class:`~http.server.HTTPServer` because a health endpoint must answer
    while it is being polled by several clients at once.  With one thread, a
    client that opens a connection and stalls holds the accept loop, and every
    subsequent probe -- an orchestrator's liveness poll, a container
    ``HEALTHCHECK``, a workflow assertion -- queues behind it and eventually
    times out.  A liveness probe that a slow client can silence reports the
    opposite of the truth.

    Four class attributes are set explicitly rather than inherited, because
    each one is load-bearing and two of them differ from the defaults:

    ``daemon_threads = True``
        Request threads must never keep the interpreter alive after the main
        thread has finished.  The base class already sets this; it is repeated
        here so the guarantee is visible at the point that depends on it rather
        than inferred from a superclass.

    ``block_on_close = False``
        **Changed from the default**, and the single change that makes shutdown
        provably prompt.  With the inherited ``True``,
        :meth:`~socketserver.BaseServer.server_close` joins every in-flight
        request thread -- and the handler in ``health.py`` speaks HTTP/1.1 with
        a ten-second idle timeout, so one client holding an open keep-alive
        connection would delay shutdown by up to ten seconds.  A container stop
        would then look like a hang and be escalated to ``SIGKILL``.  With
        ``False``, ``server_close`` closes the listening socket and returns
        immediately, and the daemon request threads are reaped with the
        interpreter.  The trade-off is explicit and correct for this workload:
        a probe response is a handful of bytes built with no I/O, so the only
        thing that can be interrupted is a reply to a caller that has itself
        just been told the process is going away.

    ``allow_reuse_address = True``
        ``SO_REUSEADDR``, so restarting immediately after a stop succeeds
        instead of failing while the previous socket drains ``TIME_WAIT``.  It
        does *not* let this server bind a port another process is actively
        listening on, which is what keeps the port-in-use diagnostic honest.

    ``allow_reuse_port = False``
        Stated explicitly because the default must never be relaxed here.
        ``SO_REUSEPORT`` would let a second instance bind the *same live* port
        and receive a share of the connections, so a duplicate start would
        appear to succeed while probes reached whichever instance the kernel
        chose.  Refusing it is what makes "the port is already in use" a
        reliable answer.
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    allow_reuse_port = False

    def __init__(
        self,
        server_address,
        RequestHandlerClass,  # noqa: N803 - name mandated by socketserver
        bind_and_activate=True,
    ):
        """Bind ``server_address``, selecting the family it actually needs.

        The socket family has to be decided before the base class creates the
        socket, and the base class reads it from ``self``.  Setting it here --
        before delegating -- is therefore the supported way to bind an IPv6
        address, and it keeps the choice per-instance rather than mutating a
        class attribute that a concurrently constructed server would see.

        Parameter naming follows :mod:`socketserver` exactly, capitalisation
        included, so this subclass stays a drop-in replacement for callers that
        pass by keyword.
        """
        self.address_family = _address_family(server_address[0])
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)


# ---------------------------------------------------------------------------
# Address resolution
# ---------------------------------------------------------------------------


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

    Step 3 looks redundant -- ``health.py`` guarantees usable values -- and it
    is kept deliberately.  The literal fallback is a requirement of the
    contract rather than a nicety: the endpoint must still serve when its
    configuration is absent from a container image, which is exactly the
    failure mode a health endpoint has to survive.  Belonging to the entry point
    as well as to the configuration reader means no single edit can remove it.

    :param host: bind address, or ``None`` to use the resolved configuration.
    :param port: bind port (``0`` for an ephemeral port), or ``None`` to use the
        resolved configuration.
    :returns: a ``(host, port)`` tuple with a non-empty ``str`` host and an
        ``int`` port in ``0..65535``.  Never raises, and never returns a value
        the caller has to re-check.
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

    Binding and serving are separated so that a caller can learn the port it
    actually got -- which matters when it asked for ``0`` -- and can arrange its
    own teardown, before a single request is accepted.  That is what makes the
    endpoint testable end to end without a fixed port, and it is why this
    function returns rather than blocking.

    :param host: bind address, or ``None`` for the resolved configuration.
    :param port: bind port, or ``None`` for the resolved configuration.  Pass
        ``0`` for an operating-system-assigned ephemeral port.
    :param handler_class: the request handler.  Defaults to the handler that
        implements the frozen contract; a subclass of it is the only sensible
        substitution, since the contract is defined by that class and not here.
    :param server_class: the server implementation.  Defaults to
        :class:`HealthHTTPServer`.
    :returns: a bound, listening, not-yet-serving server.  Call
        :func:`serve_until_signalled` to run it, and ``server_close()`` if you
        decide not to.
    :raises OSError: if the address cannot be bound -- typically
        ``EADDRINUSE``.  The partially built socket is closed by the base class
        before the exception leaves, so a failed call leaks no descriptor.
        :func:`main` translates this into a readable diagnostic;
        :func:`_describe_bind_failure` is what it uses to do so.
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

    :param host: the bound address, as reported by the socket.
    :param port: the bound port, as reported by the socket -- so an ephemeral
        port is logged as the number the kernel chose, not as ``0``.
    :param path: the served path; defaults to the resolved
        :data:`health.HEALTH_PATH`.
    :returns: a single line with no trailing newline, of the form
        ``listening on http://0.0.0.0:8000/health (child_repo_10_LOC 1.0.0)``.
    """
    served_path = path if path is not None else health.HEALTH_PATH
    authority = _format_authority(host, port)
    return (
        f"listening on http://{authority}{served_path}"
        f" ({health.APP_NAME} {health.APP_VERSION})"
    )



# ---------------------------------------------------------------------------
# Serving and orderly shutdown
# ---------------------------------------------------------------------------


def serve_until_signalled(server, signals=SHUTDOWN_SIGNALS):
    """Serve requests until a shutdown signal arrives, then close cleanly.

    Returns only when the listener has stopped accepting and its socket has been
    released, so a caller may immediately rebind the same port.

    The deadlock this avoids, and how
    --------------------------------

    :meth:`~socketserver.BaseServer.shutdown` stops the
    :meth:`~socketserver.BaseServer.serve_forever` loop and then *waits* for
    that loop to acknowledge.  CPython runs signal handlers on the main
    thread -- the very thread inside ``serve_forever`` -- so a handler that
    calls ``shutdown()`` directly waits for a loop it is itself blocking.  The
    process hangs, the container stop escalates to ``SIGKILL``, and the
    "orderly" shutdown was never orderly.

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

    Nothing is written to either stream here.  The start-up line is this
    process's only output, and a shutdown notice would make the log two lines
    where the contract says one.

    :param server: a bound server, as returned by :func:`create_server`.
    :param signals: the signals to treat as a shutdown request.  Defaults to
        :data:`SHUTDOWN_SIGNALS` (``SIGTERM`` and ``SIGINT``).
    :returns: :data:`EXIT_OK`.  A shutdown that was asked for is a success.
    """
    stop_requested = threading.Event()

    def _request_shutdown(signum, frame):  # noqa: ARG001 - stdlib handler signature
        """Signal handler: record the request and return immediately.

        Deliberately the smallest possible body.  Setting the event is the only
        action taken; every consequence of it happens on the helper thread
        below, on a thread that is safe to block.
        """
        stop_requested.set()

    def _drive_shutdown():
        """Park until a shutdown is requested, then stop the accept loop."""
        stop_requested.wait()
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - fail closed; see below
            # ``shutdown`` sets a flag and waits on an event, so there is no
            # ordinary failure mode.  The guard exists because this is a daemon
            # thread: an escaping exception would print a traceback that looked
            # like an application fault, and the ``finally`` block below closes
            # the socket regardless.  Swallowing it keeps the exit clean.
            pass

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

    return EXIT_OK


# ---------------------------------------------------------------------------
# Bind failure reporting
# ---------------------------------------------------------------------------


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
    privileged-port case is distinguishable only by its value.

    :param port: the port that could not be bound.
    :param error: the :exc:`OSError` the bind raised.
    :returns: a two-item tuple of short, complete sentences.
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

    # Anything else: report the operating system's own wording, which is more
    # informative than a guess, and still point at the two knobs that exist.
    reason = getattr(error, "strerror", None) or str(error) or "bind failed"
    return (
        reason,
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Bind, announce, serve, and return this process's exit status.

    The whole of the entry point, in the order it happens:

    1. resolve the address to bind (:func:`resolve_bind_address`);
    2. bind it, or report why not and return :data:`EXIT_BIND_FAILURE`;
    3. announce the bound address in exactly one flushed line;
    4. serve until ``SIGTERM`` or ``SIGINT``, then close and return
       :data:`EXIT_OK`.

    Step 2 never falls back to a different port.  A probe in this composition
    always targets a known port -- 8000 here -- so a silent move would replace a
    clear failure with a confusing one: a process that appeared to start while
    every probe against it timed out.

    There is no command-line parsing, by design.  Configuration is the
    environment and ``config/health.json``, resolved by ``health.py``; adding
    flags would create a third source of truth for values that already have
    exactly one.  ``python server.py`` takes no arguments and needs none.

    :returns: :data:`EXIT_OK` after an orderly shutdown, or
        :data:`EXIT_BIND_FAILURE` if the listener could not be bound.
    """
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
