# chile_repo_10_LOC

## Health endpoint

This repository's Python application answers a read-only health check at
`GET /health`. The listener is opt-in: it starts only when `app.py` is run with
`--serve`, so every invocation that worked before this endpoint existed still
behaves exactly as it did.

### Route

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Returns the health document |
| `HEAD` | `/health` | Returns the `GET` headers with no body |

`HEAD` is answered wherever `GET` is. A query string is ignored when the route
is matched, so `GET /health?probe=lb` is answered exactly like `GET /health`.

### Response

`GET /health` returns `200 OK` and this compact JSON document:

```json
{"name":"child_repo_10_LOC","version":"1.0.0","timestamp":"2026-07-31T07:34:07Z","status":"UP"}
```

| Field | Type | Value |
|---|---|---|
| `name` | string | `child_repo_10_LOC` — this application's name |
| `version` | string | `1.0.0` — this application's version |
| `timestamp` | string | The UTC instant at which the request was answered |
| `status` | string | `UP` — the literal healthy value |

All four values are JSON strings, the keys are always serialised in the order
`name`, `version`, `timestamp`, `status`, and the body carries no whitespace
between tokens: the document above is exactly 95 bytes. Those four fields are
the whole body — nothing else is reported.

`name` and `version` come from the `APP_NAME` and `APP_VERSION` module
constants at the top of `app.py`. They are declared there, rather than in a
manifest, because this repository deliberately has none.

`timestamp` is generated per request as a second-precision UTC instant of the
form `YYYY-MM-DDTHH:MM:SSZ`, matching
`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`.

### Response headers

Every answer, success or error, carries the same three header fields:

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `Content-Length` | The body's length in bytes |
| `Cache-Control` | `no-store` |

`Cache-Control: no-store` is mandatory here. The timestamp is generated per
request, so a cached liveness answer would be worse than no answer at all; the
directive stops any intermediary from retaining a stale one.

Three further fields are sent. `Connection: close`, because every answer ends its
own connection; `Date`, which the standard library writes for every response
rather than this application, and which RFC 9110 asks of a server that has a
clock; and a `Server` field naming this application and its version, which says
nothing about the interpreter underneath it: it reads `child_repo_10_LOC/1.0.0`,
where the stock handler would have appended `Python/3.x.y`. The standard library
builds that last field by joining the application's name and version with the
interpreter banner, which this handler empties, so the field arrives with the
join's separating space still on the end. RFC 9110 excludes the whitespace around
a field value from the value, so compare it as HTTP defines one rather than byte
for byte. A `405` carries one field more, `Allow: GET, HEAD`, as the table below
shows.

### Status semantics

| Request | Response |
|---|---|
| `GET /health` | `200 OK` with the four-field body above |
| `HEAD /health` | `200 OK`, the same headers (including the `Content-Length` a `GET` would return), and a zero-byte body |
| Any other method on `/health` | `405 Method Not Allowed` with `Allow: GET, HEAD` and body `{"error":"Method Not Allowed"}` |
| Any other path | `404 Not Found` with body `{"error":"Not Found"}` |

Both error bodies are fixed strings. Neither ever repeats the path that was
asked for or the method that was used, and neither is ever HTML. An unrecognised
method — `OPTIONS`, or a verb invented on the spot — is answered `405` with the
same JSON envelope and the same `Allow` field, never `501`.

### Running the server

```bash
python3 app.py --serve
```

One startup line names the address it bound, and it then serves until
interrupted with `Ctrl-C`:

```text
child_repo_10_LOC 1.0.0 listening on http://127.0.0.1:8000/health
```

Probe it from another shell:

```bash
curl -s http://127.0.0.1:8000/health
```

### Configuration

Two optional environment variables are read. Both have defaults, so `--serve`
needs no configuration at all.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | TCP port to bind; `0` requests an ephemeral port from the operating system |
| `HOST` | `127.0.0.1` | Bind address; loopback by default, so the listener is not exposed on external interfaces unless an operator opts in |

```bash
PORT=8000 python3 app.py --serve
```

`PORT` is read as an unsigned run of ASCII decimal digits, and it is the
**significant** digits — whatever is left once any leading zeros are dropped —
that have to name a value from `0` to `65535`. Leading zeros are ignored however
many of them there are, so `PORT=000080` and a value padded with four thousand
zeros both name port 80, while a value of nothing but zeros names port `0` and so
requests an ephemeral one. Every value that fails that reading falls back to
`8000` rather than failing to start — blank, non-numeric such as `8000abc`,
signed such as `+8000`, separated such as `80_00`, fractional such as `8.5`,
written in the digits of another script, and any significant run out of range,
whether that is `65536`, `99999` or four thousand nines. None of them raises, so
a malformed value never aborts start-up and never prints a traceback:

```bash
PORT=65536 python3 app.py --serve   # binds 8000, the documented fallback
```

A blank or whitespace-only `HOST` falls back to loopback for the same reason.
The three applications of this composition default to different ports — 3000,
8000 and 8080 — so all three can serve `/health` side by side on one host.

### Existing behaviour is unchanged

Run with no arguments, `app.py` does exactly what it always did:

```bash
python3 app.py
```

```text
Hello Lakshya
```

It prints that one line and exits `0`. The HTTP listener starts **only** when
`--serve` is passed, which is the whole reason the flag exists: adding the
endpoint changed no existing behaviour. Importing the module is side-effect free
as well — `python3 -c "import app"` neither prints anything nor binds a socket,
and the suite asserts both in a fresh interpreter. CPython does cache bytecode
for whatever it imports, so run that check in the residue-free form given under
**Keeping the tree clean** below.

### Tests

The suite is run by hand from this directory; nothing runs it automatically.

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m unittest
```

The cache prefix is part of the command, not a refinement of it. The runner
imports `test_app.py` and `app.py` before any test code executes, and CPython
writes their bytecode beside the sources by default — so a bare
`python3 -m unittest` leaves an untracked `__pycache__` directory behind, in a
repository whose working tree is expected to stay clean, and no test could
prevent it. Directing the cache out of the tree is what keeps the run
residue-free; `python3 -B -m unittest` and `PYTHONDONTWRITEBYTECODE=1` have the
same effect by writing no bytecode at all. Bare `python3 -m unittest` still
works and still passes — it is simply not clean-tree safe, so see **Keeping the
tree clean** below if you use it.

Default discovery finds `test_app.py` beside `app.py` and reports `Ran 6 tests`
followed by `OK`: six tests across three cases, covering the pre-existing
`greet` behaviour **and the no-argument program itself** — run in a real child
process, with its standard output, its standard error and its exit status all
compared byte for byte, so that `Hello Lakshya` is proven rather than assumed
and a listener that started without the flag would fail the suite — **and a
fresh interpreter asked to do nothing but `import app`**, which must print
nothing on either stream and exit `0`, so an import-time side effect fails the
suite rather than hiding inside it. Beyond that: the health document with its
timestamp grammar, the `PORT` and `HOST` fallbacks for every malformed value,
and the live endpoint over HTTP including `HEAD`, `404`, `405` and a caller that
hangs up mid-exchange. It is built only from `unittest` and the rest of the
standard library, and the server it exercises is bound on port `0`, so the suite
takes an ephemeral port and never collides with a running `--serve`. Every child
process it starts is started with `-B`, so no child writes bytecode of its own,
and no test writes into `os.environ`: the two resolvers are given the mapping
they read, so a case cannot leak into the server thread, into a later test or
into any process started afterwards.

A syntax-only check, if that is all you need:

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile app.py
```

### Keeping the tree clean

Every command that imports or compiles this module makes CPython write bytecode,
and by default it lands in a `__pycache__` directory beside the sources —
untracked files in a repository whose working tree is expected to stay clean.
The commands above already send that cache outside the tree; do the same for any
other invocation:

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -c "import app"
```

or delete what was written once you are finished:

```bash
rm -rf __pycache__
```

Nothing else needs tidying: the application writes no file of its own and the
suite only binds a socket, so `git status --porcelain --untracked-files=all`
should be empty again afterwards.

### Requirements

- **Python `>= 3.8`.** `ThreadingHTTPServer` needs 3.7 or newer and the existing
  f-string needs 3.6 or newer, so 3.8 is a conservative floor above both. The
  floor is recorded here because this repository has no `pyproject.toml`,
  `setup.py` or `requirements.txt` in which to declare it.
- **No dependencies.** The application and its suite are built entirely from
  Python's standard library, so there is nothing to install and no lock file to
  resolve: `python3 app.py` works immediately after checkout.

### Operational notes

The endpoint is served by the standard library's `http.server`, which Python's
own documentation says "is not recommended for production. It only implements
basic security checks." Replacing it would mean taking on a third-party server,
which this repository's zero-dependency posture rules out, so the exposure is
bounded instead:

- the listener binds `127.0.0.1` unless an operator sets `HOST`;
- each connection is served by its own thread, which is what
  `ThreadingHTTPServer` means: a caller that connects without sending anything
  holds one thread until it hangs up, so the loopback-only default above is also
  what bounds who can open them. The endpoint keeps answering normally while they
  are held, and every thread is released as soon as its caller goes away;
- the body carries only the four fields above, and no request data is ever
  echoed back on any status path;
- the response names this application and its version, and nothing about the
  runtime it happens to be built on;
- the endpoint reads no request body and exposes no dynamic input path;
- nothing about a caller reaches the process output: the access log is
  suppressed, and a caller who hangs up mid-exchange is absorbed silently rather
  than reported with its address and a traceback, so `--serve` prints its single
  startup banner and nothing else for as long as it runs.

The nested submodule serves the same four-field `/health` contract from its own
application; see `nested_child_repo_10_LOC/README.md` for its own commands and
defaults.
