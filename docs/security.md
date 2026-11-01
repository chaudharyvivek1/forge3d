# Security

forge3d runs code and writes files on your machine. This page states exactly what
that means, what the defaults protect you from, and what they do not.

The short version: **a stdio server you started yourself is trusted; anything
reachable over a network is not.** The defaults are built around that split.

## Threat model

| Deployment | Trusted caller? | Default protection |
|---|---|---|
| `forge3d-mcp` over stdio (local agent) | yes, by construction | code runs in-process |
| `forge3d-mcp --http 8000` | only if you set a token | loopback bind, token required for public, subprocess isolation |
| Inside a container / agent sandbox | depends on your runtime | subprocess isolation, rlimits, no auth unless configured |
| Hosted worker (`examples/hosted_worker.py`) | **no** | development harness — do not expose it |

The distinction matters because every tool is equally callable. `create_model`
with `backend="procedural"` executes Python you or an agent wrote. If an untrusted
party can reach that tool, they can run code as the server user.

## Agent code execution

`procedural`, `cadquery` and `blender` all run code the agent wrote. Three layers
control that:

### 1. The gate — `FORGE3D_ALLOW_CODE_EXEC`

Set it to `false` and the procedural backend refuses outright. This is a policy
switch, **not a sandbox**: while it is `true`, code runs with the server's
permissions.

### 2. The mode — `FORGE3D_EXEC_MODE`

| Mode | Isolation | Cancellation | Startup |
|---|---|---|---|
| `inprocess` | none — `exec()` in the server process | none: a timeout *abandons* the work, which keeps running | instant |
| `subprocess` | separate process, POSIX rlimits | **real** — the parent kills the child | ~1 s |
| `auto` (default) | `inprocess` for stdio, `subprocess` for HTTP | as above | — |

`inprocess` is the default for stdio because that is the "I started this server
for my own agent" case, where a second of startup per generation is pure cost.

`subprocess` mode runs `python -m forge3d.sandbox_runner`, which:

- applies `RLIMIT_CPU` (from `FORGE3D_CODE_TIMEOUT`) and `RLIMIT_CORE=0`;
- applies `RLIMIT_AS` when `FORGE3D_MEM_LIMIT_MB` is set (default `0` = off);
- writes the mesh to a path the parent gave it, and nothing else is trusted —
  not stdout, not the exit code alone;
- dies with a `SIGKILL` from the parent when the wall clock runs out.

**What `subprocess` still does not do:** it is not a security boundary against a
determined attacker. A child process can still read files the user can read, open
sockets, and `fork`. rlimits bound CPU, address space and core dumps, not
syscalls. For genuine containment you need a container, a seccomp/landlock
profile, or a dedicated sandbox runtime — which is why the Dockerfile runs as
non-root and why HTTP mode forces subprocess isolation rather than claiming to be
safe on its own.

### 2b. OS-level sandbox — `FORGE3D_SANDBOX`

`auto` (default) uses **bubblewrap** when it is installed, which gives real
filesystem and network isolation for unprivileged users:

```
bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp \
      --unshare-net --unshare-pid --unshare-uts --unshare-ipc \
      --die-with-parent --new-session \
      --bind <workdir> <workdir> --chdir <workdir> -- python -m forge3d.sandbox_runner ...
```

The child sees a read-only root, a private `/tmp`, no network, its own PID
namespace, and write access only to the work directory. `FORGE3D_SANDBOX=unshare`
opts into `unshare --net` instead (needs capabilities that most containers
withhold, so it is never attempted by default). `FORGE3D_SANDBOX=none` turns it
off.

The job metadata records `isolation` — `bwrap`, `unshare-net` or `rlimit` — so an
audit reads what actually ran, not what was configured. `forge3d doctor` reports
the same, plus a self-test that executes a trivial command through the wrapper
rather than assuming it works. A sandbox that silently fails to apply is worse
than none.

### 2c. Resource limits

In subprocess mode the child sets, after importing its heavy dependencies:

| Limit | Value | Why |
|---|---|---|
| `RLIMIT_CPU` | `FORGE3D_CODE_TIMEOUT` | a slow build cannot monopolise a core |
| `RLIMIT_AS` | `FORGE3D_MEM_LIMIT_MB` (default 0 = off) | bounds address space |
| `RLIMIT_FSIZE` | 512 MiB | a runaway loop cannot fill the disk |
| `RLIMIT_NOFILE` | 256 | no thousand-socket fan-out |
| `RLIMIT_CORE` | 0 | no core dumps on disk |

### 2d. Environment scrubbing

The child never inherits the parent's environment wholesale. Anything matching
`KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|PRIVATE|SESSION|COOKIE` is dropped, so
`FORGE3D_HOSTED_KEY` and cloud credentials are not reachable from agent-written
code. What survives is what a process needs to *run*: `PATH`, `HOME`, the
`PYTHON*` family, locale, temp dirs.

That last part is not cosmetic. A first attempt used a fixed allowlist and broke
`pip install --user` installs, because `PYTHONUSERBASE` was dropped and the child
could no longer import forge3d at all. Hardening that breaks the product is not
hardening; the policy is now pattern-based with an explicit deny list, and a test
asserts secrets do not survive while the interpreter still works.

### 3. The recommendation

- Local stdio server for your own agent → defaults are fine.
- Shared or remote → **run it in a container, set a token, keep
  `FORGE3D_ALLOW_CODE_EXEC=false` unless the caller is trusted**, and put it
  behind a reverse proxy that terminates TLS.
- Never expose the procedural backend to anonymous internet users. No setting in
  this repository makes that safe.

## HTTP transport

`forge3d-mcp --http <port>` used to bind `0.0.0.0` with no authentication. It now:

1. **binds `127.0.0.1` by default** — unreachable from the network unless you ask;
2. **refuses a public bind without a token**, exiting with a clear message;
3. **requires `Authorization: Bearer <token>`** on every request when a token is
   set, compared with `hmac.compare_digest` so the check is constant-time;
4. **leaves `/health` unauthenticated** so a load balancer can probe it;
5. **forces `subprocess` exec mode**, because an HTTP caller is not assumed
   trustworthy.

```bash
# local only, no auth needed
forge3d-mcp --http 8000

# reachable on the network, authenticated
FORGE3D_HTTP_TOKEN=$(openssl rand -hex 32) forge3d-mcp --http 8000 --host 0.0.0.0

# you have been warned
forge3d-mcp --http 8000 --host 0.0.0.0 --allow-unauthenticated
```

The token is a shared secret, not a user system. There are no per-user
identities, no scopes, no audit log of who called what. If you need those, put an
OAuth proxy in front and keep this server on loopback behind it.

**Implemented in-process:** a token-bucket rate limiter
(`FORGE3D_RATE_LIMIT`, requests per minute per token/IP, `429` + `Retry-After`),
bearer authentication, and a bounded concurrency queue.

**Still not implemented:** TLS termination, per-user identity, CORS policy beyond
fastmcp's defaults, and request-size limits. The rate limiter is per-process and
in-memory, so it does not coordinate across instances — use a reverse proxy
(nginx, Caddy, Traefik) for all four.

## One policy for every code-running backend

Three backends run caller-supplied code: `procedural`, `cadquery` and `blender`.
Each declares `executes_user_code = True`, and the registry refuses any of them
when `FORGE3D_ALLOW_CODE_EXEC=false` — the check is in the single dispatch point,
not duplicated per backend.

That matters because it was previously duplicated: only `procedural` checked, so
disabling code execution still left CadQuery executing in-process with no timeout
and Blender running unsandboxed. Both now honour `FORGE3D_EXEC_MODE` and
`FORGE3D_SANDBOX` like the procedural backend does.

`openscad` is marked `executes_user_code = False` because its source is a
declarative DSL rather than Python. That is not a claim of safety: OpenSCAD's
`import()` can read files the service user can read.

## Remote images are fetched through a guard

The image backend accepts URLs. `requests.get(url)` + `.content` — the obvious
implementation, and the one this repo shipped — is a server-side request forgery
primitive and a memory-exhaustion bug. `netguard.py` now enforces:

- **output paths**: every tool that writes a file resolves its destination and
  refuses anything outside `FORGE3D_WORKDIR`, following symlinks. Without this a
  caller could ask for a viewer at `/etc/cron.d/x`. `FORGE3D_ALLOW_OUTSIDE_WORKDIR=1`
  disables it for local CLI scripts — never set it on a server;
- **scheme**: http/https only;
- **address pinning**: the validated IP is the IP that is dialled, with the
  hostname kept for TLS verification — resolving a name and then handing the name
  to an HTTP client is a time-of-check/time-of-use hole (DNS rebinding);
- **address**: an address is allowed only if it is **globally routable**
  (`is_global`). That is a positive rule rather than a blocklist, because a
  blocklist misses ranges like `100.64.0.0/10` (carrier-grade NAT), which is
  neither `is_private` nor `is_reserved`. Checked after **every redirect hop**;
- **size**: streamed with a hard byte ceiling (`FORGE3D_MAX_DOWNLOAD_MB` applies to
  provider downloads; the image path caps at 25 MiB);
- **content**: the declared type must be an image, the bytes must decode, and the
  pixel count must stay inside a budget so a decompression bomb is refused.

A refusal names the reason and suggests passing a local path instead.

**Provider asset downloads go through the same guard.** A provider — or a
compromised generic worker — can return any URL it likes, including
`http://127.0.0.1:9200/`. Size caps and destination checks solve different
problems, so both apply: `FORGE3D_MAX_DOWNLOAD_MB` bounds the bytes and the guard
bounds the destination, with an optional
`FORGE3D_TRUSTED_DOWNLOAD_HOSTS=cdn.meshy.ai,...` allowlist.

**Proxies are the honest caveat.** An HTTP proxy performs its own DNS resolution,
so IP pinning cannot apply through one. Proxied fetches are still destination
-checked and are marked `proxied=True`; `FORGE3D_ALLOW_PROXY_FETCH=false` refuses
them outright.

## Paid submissions are not retried blindly

A job-creation POST that times out may already have created a job. Retrying it can
buy a second generation. Submission therefore:

- sends an `Idempotency-Key` header (providers that support it will dedupe);
- is **never** retried after an ambiguous timeout or a 5xx;
- raises `AmbiguousSubmission` carrying that key — including on a **5xx**, which
  may mean "created, then failed to answer";
- the backend reports `status: uncertain`, the job store persists that state, and
  `create_model` returns it with the key. It is deliberately *not* `failed`:
  retrying a failed job is normal, retrying an uncertain one costs money.

Ordinary GETs are still retried with jittered backoff.

## Bubblewrap is not a confidentiality boundary

The sandbox binds the host root read-only. That stops writes; it does **not** make
host files unreadable. Anything the service user can read, the sandboxed child can
read. Treat it as an integrity and blast-radius control, not a secrecy one.

## Filesystem

- Tools write only under `FORGE3D_WORKDIR` (default `.forge3d/`), except when you
  pass an explicit `out_path`/`out_html` — those are honoured verbatim, so do not
  pass agent-chosen absolute paths from untrusted input.
- Model names are slugified: path separators, control characters and `..` are
  stripped, and Windows device names (`con`, `aux`, `com1`…) are suffixed. This is
  a traversal guard, not a permission system.
- Job records and generated files are written atomically (temp file +
  `os.replace`), so a crash cannot leave a half-written mesh that later validates
  as a corrupt one.
- Each generation writes to `models/<name>/<job-id>/`, so two concurrent runs
  cannot overwrite each other's output.

## Secrets

`doctor` reports whether an environment variable is **set**, never its value, so
`FORGE3D_HOSTED_KEY` and `FORGE3D_HTTP_TOKEN` are not echoed into an agent's
context. Provider keys stay in the worker process; forge3d only sends the bearer
token you configured.

## Supply chain

- Dependencies are ordinary PyPI packages: `trimesh`, `numpy`, `scipy`,
  `fastmcp`, `requests`, `pillow`, plus optional `matplotlib`, `cadquery`,
  `uvicorn`.
- Generated HTML loads three.js from `unpkg.com` by default. That is a third-party
  script in a page you may hand to a client — for anything sensitive, mirror three
  locally and set `FORGE3D_THREE_URL`, or use the `forge3d-viewer` custom element
  with your own bundler.
- The viewer escapes model names and inline JSON, so a hostile model name cannot
  break out of the page's script element and execute in the browser.

## Reporting

This is a personal open-source project, not a product with an SLA. If you find a security problem, open an issue that describes the impact without a
working reproduction, or contact the maintainer directly for anything you would
rather not publish.
