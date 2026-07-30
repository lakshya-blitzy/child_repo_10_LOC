# Container image for the Python health-endpoint tier (child_repo_10_LOC),
# which serves the frozen /health contract on port 8000.
#
# A single stage that carries this tier's three Python files, its identity
# manifest and its serving configuration into /app, runs `python server.py`, and
# observes itself with a HEALTHCHECK written in the interpreter already in the
# image. It installs no package, adds no build tool, creates no virtual
# environment, declares no volume and carries no secret.
#
# EXAMPLE response (the timestamp is read per request, so it differs every call):
#
#   GET /health   -> 200
#                    Content-Type: application/json; charset=utf-8
#                    Cache-Control: no-store
#                    {"name":"child_repo_10_LOC","version":"1.0.0",
#                     "timestamp":"<RFC3339 UTC timestamp>","status":"UP"}
#   HEAD /health  -> 200, headers only, no body
#   POST /health  -> 405, Allow: GET, HEAD
#   any other path-> 404, {"error":"Not Found"}
#
# `name` and `version` come from pyproject.toml; `path` and `status` are frozen
# contract constants. docs/health-endpoint.md, in the apex repository, is the
# normative definition all three tiers implement.
#
# Canonical commands (keep these in step with the sibling README.md):
#
#   docker build -t child_repo_10_LOC:1.0.0 .
#   docker run --rm --name child_health -p 8000:8000 child_repo_10_LOC:1.0.0
#   curl -i http://127.0.0.1:8000/health
#   docker inspect --format '{{.State.Health.Status}}' child_health
#   docker stop child_health
#
# Three constraints are not negotiable here:
#   1. Zero installed packages. This tier runs on the standard library alone
#      (json, datetime, http.server, tomllib, pathlib, signal, urllib.request),
#      all present in the base image. No requirements.txt, no pip install, no
#      apt-get, ever -- the composition's security posture rests on it.
#   2. Port 8000 is this tier's default. The apex owns 3000 and the nested Java
#      tier owns 8080, so all three can run at once on one host. HEALTH_PORT may
#      move this tier's listener and the probe below follows it, but this image
#      must never bind or address 3000 or 8080.
#   3. Single stage. The compile-then-run pattern belongs to the Java tier.
#
# This file references no path belonging to another tier; the levels share a
# documented contract, never code.

# Pinned by tag AND by digest: the tag names the intended release line, the
# digest makes the build reproducible if the tag is later republished. This is
# the OCI image *index* digest for python:3.14-slim, so it resolves on every
# published architecture. The tag tracks the 3.14 series that the sibling
# .python-version pins exactly (3.14.6, the version this image ships); if either
# moves, both move and the digest is re-resolved from the registry. Never guess,
# shorten or hand-edit a digest.
#
# THE TRADE, STATED PLAINLY: a frozen base does not receive the operating-system
# patches its tag does. This file chooses reproducibility because the digest is a
# declared value in the project specification, and it discharges the resulting
# obligation here rather than leaving it implicit. The digest below was verified
# against the live registry as the current head of the tag, so this build is not
# running on superseded content. SCAN GATE: scan the BUILT image (a Dockerfile has
# no vulnerabilities, an image does); fail on a HIGH or CRITICAL finding that has a
# fix available and report anything lower as advisory; treat a fixable finding as a
# REFRESH instruction, never as a reason to un-pin - un-pinning trades a known,
# dated exposure for an unknown, undated one. REFRESH: pull python:3.14-slim, read
# `{{index .RepoDigests 0}}`, replace the digest here AND in the base.digest label,
# update the specification in the same change, rebuild, re-scan, re-assert the
# endpoint contract.
#
# There is no `# syntax=` directive on purpose: it would fetch a floating-tagged
# BuildKit frontend at build time, which contradicts pinning everything exactly.
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

# Standard OCI annotations, so a registry or host inspection identifies the image
# without reading this file. There is deliberately no
# org.opencontainers.image.version label: pyproject.toml is the single declared
# source of the version and the endpoint reports it, so a second copy here could
# drift out of step. base.name and base.digest are both declared and the pair is
# the point: the name answers which line this is built on, only the digest answers
# which content - the question a base-image advisory actually asks. The digest here
# MUST equal the one in the FROM line above; they are refreshed together.
LABEL org.opencontainers.image.title="child_repo_10_LOC" \
      org.opencontainers.image.description="Python /health endpoint serving the frozen name/version/timestamp/status contract on port 8000, on the standard library alone" \
      org.opencontainers.image.base.name="python:3.14-slim" \
      org.opencontainers.image.base.digest="sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6"

# PYTHONDONTWRITEBYTECODE keeps __pycache__ out of the image layers and the
# container filesystem; the modules are imported once at start-up, so cached
# bytecode saves nothing worth a writable directory. PYTHONUNBUFFERED makes the
# listener's start-up line -- and any configuration warning before it -- appear in
# `docker logs` immediately rather than sitting in a pipe buffer.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# The application root. health.py resolves pyproject.toml and config/health.json
# relative to its own module directory, so this layout -- not the CWD -- is what
# makes those two files findable.
WORKDIR /app

# The one and only RUN layer, and it installs nothing: a dedicated non-root
# account with fixed IDs (10001:10001, both unused in the base image) so the
# identity is deterministic across rebuilds. No home directory and a nologin
# shell, because this account exists only to own a running process.
#
# If a future edit adds a RUN that installs a package, the design has gone wrong;
# see constraint 1 above.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --no-log-init \
       --shell /usr/sbin/nologin appuser

# Five explicit paths rather than `COPY . .`, so a missing input fails the build
# loudly instead of vanishing quietly:
#
#   server.py       the entry point the CMD below runs
#   health.py       the payload builder and request handler; server.py imports it
#   app.py          this tier's pre-existing greet() behaviour, preserved
#   pyproject.toml  the declared source of `name` and `version`
#   config/         carries health.json: the declared host, port, path and status
#
# The five files this tier needs at run time: server.py (the entry point the CMD
# below runs), health.py (the payload builder and handler that server.py imports),
# app.py (this tier's pre-existing greet() behaviour, preserved unchanged),
# pyproject.toml (the declared source of `name` and `version`) and
# config/health.json (the declared host, port, path and status).
#
# The last two are the dangerous ones, because they fail SILENTLY: health.py reads
# both defensively and falls back to compiled-in literals rather than refusing to
# serve, which is correct for a health endpoint but means an image built without
# them still answers 200 with a structurally valid body. Reading the published
# payload is NOT a sufficient check and must not be mistaken for one: this tier's
# fallbacks deliberately carry the same `child_repo_10_LOC` / `1.0.0` values, so an
# image missing both documents serves a byte-identical body. Only the provenance
# checks recorded after the COPY lines can separate "read from the declared file"
# from "reconstructed from a literal".
#
# health.json is copied BY NAME to its exact destination path, and both halves
# matter. The destination keeps the subdirectory because health.py looks for
# <module dir>/config/health.json; flattening it into /app would leave the file
# present but unreachable. And naming the file rather than its directory is what
# makes its absence a BUILD failure: `COPY config/ ./config/` succeeds and produces
# an EMPTY /app/config whenever the file is missing from the context or excluded by
# .dockerignore, which is exactly the fail-silent hole above; `COPY
# config/health.json` cannot - BuildKit fails with "not found". Level 1 copies its
# own config/health.json the same way, so the two tiers fail alike.
#
# Nothing is copied with --chown, so the payload stays root-owned and
# world-readable: the account below can read the code it runs but not rewrite it.
# Everything else in this tier is kept out of the context by .dockerignore.
COPY server.py health.py app.py pyproject.toml ./
COPY config/health.json ./config/health.json

# ======================= REQUIRED IN-IMAGE VERIFICATION ======================
# Now that both COPY lines above name every file they carry, a deleted file or a
# newly broadened .dockerignore pattern fails THIS build with "not found" rather
# than producing an image that starts and misreports itself. That is the first
# half of the guarantee, and it is the half this file can enforce on its own.
#
# The second half cannot be expressed here. pyproject.toml and config/health.json
# fail SILENTLY at run time when absent, because health.py falls back to
# compiled-in literals that carry the same values deliberately - the endpoint
# still answers 200 with a valid four-member body, the identity still reads
# `child_repo_10_LOC` / `1.0.0`, and the HEALTHCHECK below still turns green,
# while nothing was actually read from the declared documents. No amount of
# inspecting the response can separate those two states. Two direct checks
# against the built image can, and both are required rather than optional (the
# automated pipeline's container job owns them, exactly as it does at Level 1):
#
#     docker run --rm <image> python -c "import pathlib, sys;
#       missing = [p for p in ('/app/server.py', '/app/health.py', '/app/app.py',
#                              '/app/pyproject.toml', '/app/config/health.json')
#                  if not pathlib.Path(p).is_file()];
#       sys.exit(f'missing: {missing}' if missing else 0)"
#     docker run --rm <image> python -c "import health, sys;
#       bad = {k: v for k, v in health.get_config_sources().items() if v != 'file'};
#       sys.exit(f'not read from file: {bad}' if bad else 0)"
#
# The second is the load-bearing one: get_config_sources() reports the PROVENANCE
# of every resolved value, so `file` proves the declared documents were read and
# `fallback` proves they were not - the distinction a green probe cannot make.
# Both must run as the unprivileged account selected below, because readability
# for uid 10001 is the property being asserted, not readability for root.
# =============================================================================

# The DEFAULT port this tier serves on. Documentation for a human and for
# `docker run -P`; publishing is still the operator's decision (`-p 8000:8000`).
# It is a declaration rather than a constraint, and the one place in this file that
# names the number: HEALTH_PORT moves the listener inside the container and the
# HEALTHCHECK below follows that override on its own, so an operator who relocates
# the listener publishes the port they chose and nothing here needs editing.
EXPOSE 8000

# Drop to the unprivileged account for everything that follows: the HEALTHCHECK
# probe and the server both run as 10001. Written numerically so an orchestrator
# enforcing "must not run as root" can verify it without resolving /etc/passwd.
# Port 8000 is unprivileged, so no capability is needed to bind it.
USER 10001:10001

# SIGTERM is already Docker's default and this base image overrides nothing, so
# this line changes no behaviour -- it states the contract the rest of the file
# depends on: SIGTERM is what server.py installs an orderly-shutdown handler for.
STOPSIGNAL SIGTERM

# HEALTHCHECK -- the container observing its own endpoint.
#
# The probe is written in the language of the application and installs nothing,
# because `curl` cannot be assumed present in a minimal image and this
# Debian-slim base carries neither `curl` nor BusyBox `wget`. Installing one to
# watch the other would break the zero-package constraint when the interpreter
# running the server can make the request itself. Exec form, so no shell is
# involved and the JSON \n escapes are real newlines by the time Python sees
# them. Read back out, the script is:
#
#     import sys
#     sys.path.insert(0, '/app')
#     try:
#         import health, urllib.request
#         URL = f'http://127.0.0.1:{health.PORT}{health.HEALTH_PATH}'
#         class _NoRedirect(urllib.request.HTTPRedirectHandler):
#             def redirect_request(self, *unused):
#                 return None
#         opener = urllib.request.build_opener(
#             urllib.request.ProxyHandler({}), _NoRedirect())
#         with opener.open(URL, timeout=2) as response:
#             sys.exit(0 if response.status == 200
#                      and response.geturl() == URL else 1)
#     except Exception:
#         sys.exit(1)
#
# Everything sits inside the try, the imports included, for one reason: a probe
# must never print. An import that failed outside it would exit 1 anyway - Python's
# status for an unhandled exception - but it would print a traceback to the
# container's log on every check, and a probe that narrates its own failure is
# worse than one that reports it. `sys.path.insert(0, '/app')` makes the import
# independent of the directory the runtime starts the probe in, rather than relying
# on WORKDIR remaining what it is above.
#
# The probe opens the URL through an opener it builds itself rather than through
# `urllib.request.urlopen`, and the two substituted handlers are the whole reason.
# A probe's one job is to report the state of *this* process, and the default
# opener can be pointed elsewhere by two mechanisms outside this file's control:
#
#   * `ProxyHandler({})` - an EMPTY mapping, the documented way to disable proxy
#     autodetection entirely. The default opener reads `http_proxy`/`HTTP_PROXY`
#     from the environment, and `docker run -e` sets the environment. Measured with
#     the endpoint stopped and a 200-answering listener in `http_proxy`: the
#     autodetecting form exits 0 - a dead container reporting healthy - while this
#     form exits 1. The mapping is empty rather than a `no_proxy` entry, because
#     `no_proxy` would be defending an environment variable with another one.
#   * `_NoRedirect` - `redirect_request` returning None makes the handler decline,
#     so a 3xx is raised as `HTTPError` instead of followed. Measured against a
#     `302` towards a 200: the following form exits 0, this form exits 1. A liveness
#     answer must come from the process being asked.
#
# The `geturl()` equality states the same assertion positively: the body that
# satisfied this probe must have arrived from the exact URL it asked for. There is
# no DNS in the path either - the authority is a literal address.
#
# Exit 0 is healthy and 1 unhealthy, and those are the only two codes it can
# produce: the runtime reserves 2. The probe FAILS CLOSED - every failure mode
# lands on 1 and prints nothing: a refused connection, a socket error, a timeout,
# a non-200 status and a declined redirect (urllib raises HTTPError for those) are
# all caught, and `except Exception` cannot swallow the sys.exit(0) above it
# because SystemExit descends from BaseException.
#
# The 2s request timeout sits inside the 3s probe timeout, so the probe always
# answers rather than being killed mid-request, and 3s is strictly below the 30s
# interval so probes cannot overlap. A 5s start period is ample for this
# interpreter (the 20s grace window belongs to the JVM tier).
#
# The address is 127.0.0.1 because a health probe asserts the state of *this*
# process inside *this* container, never something reachable over the network.
#
# The port and the path are NOT restated here; they are read from health.py, the
# single module that resolves them, through the single documented chain
# (HEALTH_PORT, then config/health.json, then the compiled-in literal). This image
# honours HEALTH_PORT at start-up, so a probe holding its own copy of 8000 would
# report a perfectly healthy container as unhealthy the moment anyone used the
# override this tier documents - the container would be killed for obeying its own
# configuration. Importing the resolver instead of guessing also makes disagreement
# structurally impossible rather than merely unlikely: a value the chain refuses
# (`HEALTH_PORT=0`, out of range, unparseable) falls through the same way on both
# sides, so probe and server land on the same port even when the environment is
# wrong. That is the same reason server.py does not re-derive it either.
# Verified in both directions on the built image: healthy with the default
# configuration, and healthy again under `-e HEALTH_PORT=8100`, where a probe
# holding the literal reported the container unhealthy.
#
# health.py is imported rather than server.py: server.py binds a listener when it
# runs, so a probe that reached for it would open a second socket on every check.
# Importing health.py binds nothing and is byte-silent on both streams (measured:
# 0 bytes out, 0 bytes err), so it adds no line to the log and no work beyond the
# two small configuration reads the server already performs.
#
# The host, by contrast, is written rather than resolved, and deliberately so:
# HEALTH_HOST moves the *bind* address, and 0.0.0.0 is a bind address rather than a
# destination, so following it would point the probe at something other than this
# process. Loopback is always where this process is.
#
# Deliberately status-only: the probe asserts liveness, not payload identity.
# Parsing the body would add work to a check that runs forever and would fail a
# container that is genuinely serving from degraded configuration.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys\nsys.path.insert(0, '/app')\ntry:\n    import health, urllib.request\n    URL = f'http://127.0.0.1:{health.PORT}{health.HEALTH_PATH}'\n    class _NoRedirect(urllib.request.HTTPRedirectHandler):\n        def redirect_request(self, *unused):\n            return None\n    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())\n    with opener.open(URL, timeout=2) as response:\n        sys.exit(0 if response.status == 200 and response.geturl() == URL else 1)\nexcept Exception:\n    sys.exit(1)"]

# Exec form, and it matters: `python` becomes PID 1, so a `docker stop` delivers
# SIGTERM straight to the interpreter and server.py's handler stops the listener,
# joins its in-flight request threads and exits 0. The shell form would interpose
# /bin/sh -c, which does not forward the signal, and every stop would become a
# ten-second wait followed by SIGKILL. There is no ENTRYPOINT wrapper, no tini,
# no dumb-init and no supervisor -- server.py handles SIGTERM and SIGINT itself.
#
# It is server.py and never app.py: app.py prints one greeting and exits, so a
# container started on it would be dead before a probe could reach it. It takes
# no arguments by design, because configuration comes from the environment and
# from config/health.json, and a flag would create a third source of truth.
CMD ["python", "server.py"]
