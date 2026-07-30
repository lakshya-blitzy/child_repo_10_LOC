# Dockerfile - container image for the Python health-endpoint tier
# (child_repo_10_LOC), which serves the frozen /health contract on port 8000.
#
# ---------------------------------------------------------------------------
# WHAT THIS IMAGE IS
#
# A single-stage image that carries this tier's three Python files, its declared
# identity manifest and its serving configuration into /app, runs the long-lived
# listener with `python server.py`, and observes itself through a HEALTHCHECK
# that calls its own /health endpoint with the interpreter that is already in
# the image. That is the whole of it. This image installs no package of any
# kind, adds no build tool, creates no virtual environment, declares no volume
# and carries no secret.
#
# WHY THIS FILE IS A CREATE RATHER THAN AN EDIT
#
# This composition had no container artifact of any kind before the /health
# feature: a twelve-pattern probe for Dockerfile*, *.dockerfile, Containerfile*,
# docker-compose*, compose.y*ml, .dockerignore, devcontainer.json and others
# matched nothing at any of the three tiers, so there was nothing to modify. A
# container definition is nevertheless required, because the container is the
# only place a HEALTHCHECK can express "this application is up" -- which is
# precisely the semantic the /health endpoint introduces.
#
# THE CONTRACT THE RUNNING IMAGE SERVES
#
#   GET /health   -> 200
#                    Content-Type: application/json; charset=utf-8
#                    Cache-Control: no-store
#                    {"name":"child_repo_10_LOC","version":"1.0.0",
#                     "timestamp":"2026-07-30T00:42:05.351Z","status":"UP"}
#   HEAD /health  -> 200, headers only, no body
#   POST /health  -> 405, Allow: GET, HEAD
#   any other path-> 404, {"error":"Not Found"}
#
# `name` and `version` are read from pyproject.toml; `path` and `status` are
# frozen contract constants that config/health.json restates exactly; the
# `timestamp` is read from the clock on every request, so two consecutive calls
# always differ while the other three fields stay stable. The normative
# definition of the whole contract lives in the apex document
# docs/health-endpoint.md, which all three tiers implement identically.
#
# CANONICAL COMMANDS (keep these in step with the sibling README.md)
#
#   docker build -t child_repo_10_LOC:1.0.0 .
#   docker run --rm --name child_health -p 8000:8000 child_repo_10_LOC:1.0.0
#   curl -i http://127.0.0.1:8000/health
#   docker inspect --format '{{.State.Health.Status}}' child_health
#   docker stop child_health      # orderly: SIGTERM, then exit 0 in ~0.2s
#
# THREE CONSTRAINTS THAT ARE NOT NEGOTIABLE HERE
#
#   1. Zero installed packages. This tier runs on the Python standard library
#      alone -- json, datetime, http.server, tomllib, pathlib, signal and
#      urllib.request, all present in the base image. There is no
#      requirements.txt, no pip install, no apt-get, and none may be added:
#      the composition's security posture rests on having no third-party
#      runtime dependency at all, and that property is asserted in CI.
#   2. Port 8000 only. Each tier of this composition owns a distinct default
#      port so that all three can run at once on one host: 3000 is the
#      JavaScript apex, 8000 is this tier, 8080 is the nested Java tier.
#   3. Single stage. The multi-stage compile-then-run pattern belongs to the
#      Java tier, which needs a JDK to build and only a JRE to run. Python
#      needs no compile step, so a second stage would add moving parts and buy
#      nothing.
#
# This file references no path and no artifact belonging to another tier. The
# three levels are runtime-independent by design; they share a documented
# contract, never code.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Base image
#
# Pinned by tag AND by digest. The tag names the intended release line; the
# digest makes the build reproducible even if the tag is later republished.
#
# The digest below is the OCI image *index* digest that Docker Hub currently
# serves for the `python:3.14-slim` tag, confirmed two ways before it was
# written here: the registry returned it as `docker-content-digest` with
# content-type application/vnd.oci.image.index.v1+json, and the locally pulled
# image reports the identical value in RepoDigests. Being an index digest
# rather than a per-platform manifest digest, it resolves on every published
# architecture, so it is safe for CI runners as well as for this host.
#
# The tag deliberately tracks the 3.14 series, which is the series the sibling
# .python-version pins exactly (3.14.6, the version this base image ships). If
# one of the two ever moves, both move together -- and the digest here has to
# be re-resolved from the registry at the same time. Never guess, shorten or
# hand-edit a digest: a wrong one fails the build in a confusing way, and a
# bare pinned tag would be preferable to a fabricated digest.
#
# There is no `# syntax=` directive above on purpose. It would fetch an
# external, floating-tagged BuildKit frontend at build time, which contradicts
# both the "every version is an exact pin" rule and the goal of a build that
# needs nothing from the network beyond this one base image.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

# ---------------------------------------------------------------------------
# Image metadata
#
# Standard OCI annotations, so that anyone inspecting a registry or a running
# host can tell what this image is without reading the Dockerfile.
#
# There is deliberately no org.opencontainers.image.version label. pyproject.toml
# is the single declared source of this application's version, the running
# endpoint reports it in every response, and a second copy here could silently
# drift out of step with the first. Nothing secret appears in any label, and
# nothing here is read at runtime.
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="child_repo_10_LOC" \
      org.opencontainers.image.description="Python /health endpoint serving the frozen name/version/timestamp/status contract on port 8000, on the standard library alone" \
      org.opencontainers.image.base.name="python:3.14-slim"

# ---------------------------------------------------------------------------
# Interpreter behaviour
#
# PYTHONDONTWRITEBYTECODE keeps __pycache__ directories out of the image layers
# and out of the container's filesystem. The modules here are imported once at
# start-up, so cached bytecode saves nothing worth a writable directory.
#
# PYTHONUNBUFFERED makes the listener's single start-up line -- and any
# sanitized configuration warning that precedes it -- appear in `docker logs`
# immediately instead of sitting in a pipe buffer. For a process whose whole
# job is to report its own state, a log line that arrives late is a log line
# that arrives too late.
#
# Both are behavioural switches for the interpreter already in the image.
# Neither installs anything, and neither carries configuration or credentials.
# ---------------------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# The application root. `health.py` resolves pyproject.toml and
# config/health.json relative to its own module directory rather than to the
# working directory, so the layout below -- not the CWD -- is what makes those
# two files findable.
WORKDIR /app

# ---------------------------------------------------------------------------
# The one and only RUN layer: an unprivileged account to serve as.
#
# This installs nothing. It creates a dedicated non-root user with fixed,
# documented IDs (10001:10001, both unused in the base image) so that the
# identity is deterministic across rebuilds and can be asserted by an
# orchestrator. No home directory is created and the shell is nologin, because
# this account exists only to own a running process -- it is never logged into.
# --no-log-init avoids initialising the sparse lastlog record, which is pure
# waste for an account that cannot log in.
#
# If a future edit to this file adds a RUN that installs a package, the design
# has gone wrong: see constraint 1 in the header.
# ---------------------------------------------------------------------------
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --no-log-init \
       --shell /usr/sbin/nologin appuser

# ---------------------------------------------------------------------------
# Application payload: five explicit paths, no `COPY . .`.
#
# Every one of the five is required at runtime, and each is listed by name so
# that this file documents its own dependencies and a missing one fails the
# build loudly instead of vanishing quietly:
#
#   server.py       The entry point the CMD below runs: binds the listener,
#                   announces the bound address, and shuts down on a signal.
#   health.py       The payload builder and the request handler. server.py
#                   imports it, so its absence is an ImportError at start-up.
#   app.py          This tier's pre-existing greet() behaviour, which the
#                   feature is required to preserve unchanged.
#   pyproject.toml  The declared source of `name` and `version`.
#   config/         Carries health.json: the declared host, port, path and
#                   status for this tier.
#
# The last two are the dangerous ones, because they fail SILENTLY. health.py
# reads both defensively and falls back to compiled-in literals rather than
# refusing to serve -- correct behaviour for a health endpoint, but it means an
# image built without them still answers 200 with a structurally valid body
# sourced from fallbacks. A green probe with the wrong identity is harder to
# notice than a crash, which is why both lines below exist and why the
# published payload is checked for the real `child_repo_10_LOC` / `1.0.0`
# values after every build.
#
# config/ is copied as a directory, preserving the subdirectory, because
# health.py looks for <module dir>/config/health.json. Flattening it into /app
# would leave the file present but unreachable -- the silent-fallback failure
# again, this time with the file sitting one directory away from where it is
# read.
#
# Nothing is copied with --chown. The payload stays owned by root and
# world-readable, so the unprivileged account selected below can read the code
# it runs but cannot rewrite it. Everything else in this tier -- test_app.py,
# README.md, .env.example, .github/, .git/ and the nested submodule -- is kept
# out of the build context by the sibling .dockerignore and has no business in
# a running container.
# ---------------------------------------------------------------------------
COPY server.py health.py app.py pyproject.toml ./
COPY config/ ./config/

# The port this tier serves on, and the only port this image ever binds or
# probes. Documentation for a human and for `docker run -P`; publishing is
# still the operator's decision (`-p 8000:8000`).
EXPOSE 8000

# Drop to the unprivileged account created above, for everything that follows:
# the HEALTHCHECK probe and the server itself both run as 10001. Written
# numerically on purpose -- an orchestrator enforcing "must not run as root"
# can verify a numeric USER without resolving /etc/passwd. Port 8000 is
# unprivileged, so no capability is needed to bind it.
USER 10001:10001

# The signal a `docker stop` sends. SIGTERM is already Docker's default and
# this base image overrides nothing, so this line changes no behaviour -- it
# states the contract that the rest of the file depends on: SIGTERM is what
# server.py installs an orderly-shutdown handler for.
STOPSIGNAL SIGTERM

# ---------------------------------------------------------------------------
# HEALTHCHECK - the container observing its own endpoint
#
# The probe is written in the language of the application and installs nothing.
# That is not a stylistic preference: `curl` cannot be assumed present in a
# minimal image, and this Debian-slim base carries neither `curl` nor BusyBox
# `wget` (verified in the image). Installing one to watch the other would break
# the zero-package constraint for no gain, when the interpreter running the
# server can make the same request itself. `wget`, `nc` and shell /dev/tcp
# tricks are ruled out for the same reason -- the first two are absent and the
# third needs a shell this probe does not use.
#
# Exec form, so no shell is involved and no shell-quoting hazard exists; the
# JSON \n escapes are real newlines by the time Python sees them. Reading the
# script back out:
#
#     import sys, urllib.request
#     try:
#         with urllib.request.urlopen("http://127.0.0.1:8000/health",
#                                     timeout=2) as response:
#             sys.exit(0 if response.status == 200 else 1)
#     except Exception:
#         sys.exit(1)
#
# Exit 0 means healthy and exit 1 means unhealthy; those are the only two codes
# this probe can produce, because the container runtime reserves 2 and reads
# anything else as a broken probe rather than an unhealthy container. Every
# failure mode lands on 1 and prints nothing: a refused connection, a DNS or
# socket error, a timeout, and a non-200 status (urllib raises HTTPError for
# those) are all caught by `except Exception`, which cannot swallow the
# sys.exit(0) above it because SystemExit descends from BaseException rather
# than Exception. Verified before it was written here: exit 0 against a live
# endpoint, exit 1 against a dead port, exit 1 against a non-200 path, and no
# traceback on any of the three.
#
# The request timeout is 2s, comfortably inside the 3s probe timeout, so the
# probe always answers rather than being killed mid-request; and 3s is strictly
# less than the 30s interval, so a slow probe can never overlap the next one.
# The endpoint reads one clock and does no I/O, so 30s is a generous cadence
# and a 5s start period is ample for this interpreter to import three modules
# and bind a socket -- the 20s grace window belongs to the JVM tier, which
# needs it. Three retries keep one transient failure from flipping a healthy
# container to unhealthy.
#
# The address is 127.0.0.1 because a health probe asserts the state of *this*
# process inside *this* container, never something reachable over the network.
# It is also fixed at port 8000. server.py honours HEALTH_PORT and HEALTH_HOST
# at start-up, so an image run with HEALTH_PORT set elsewhere must override
# this probe too (`docker run --health-cmd ...`); the alternative, a probe that
# guesses, would report a healthy container as unhealthy the moment the two
# disagreed.
#
# Deliberately status-only: the probe asserts liveness, not payload identity.
# Parsing the body to check `"status":"UP"` would add work to a check that runs
# forever, and would fail a container that is genuinely serving whenever its
# configuration had degraded to fallback values. Identity is verified once,
# after the build, where a wrong answer is a build defect rather than a
# liveness event.
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request\ntry:\n    with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2) as response:\n        sys.exit(0 if response.status == 200 else 1)\nexcept Exception:\n    sys.exit(1)"]

# ---------------------------------------------------------------------------
# Entry point
#
# Exec form, and that matters: it makes `python` PID 1, so a `docker stop`
# delivers SIGTERM straight to the interpreter and server.py's handler stops
# the listener, joins its in-flight request threads and exits 0 -- measured at
# about 0.2s. The shell form (`CMD python server.py`) would interpose
# /bin/sh -c, which does not forward the signal, and every container stop would
# become a ten-second wait followed by SIGKILL.
#
# There is no ENTRYPOINT wrapper, no tini, no dumb-init and no supervisor.
# server.py handles SIGTERM and SIGINT itself and needs no help reaping
# children it does not have; an init shim would only add a process between the
# runtime and the code that already does the work.
#
# It is server.py and never app.py. app.py prints one greeting and exits, so a
# container started on it would be dead before a probe could reach it. app.py
# is in the image because health.py's tier owns it and its behaviour is
# preserved, not because anything starts it.
#
# `python server.py` takes no arguments by design: configuration comes from the
# environment and from config/health.json, and a command-line flag would create
# a third source of truth for values that already have exactly one.
# ---------------------------------------------------------------------------
CMD ["python", "server.py"]
