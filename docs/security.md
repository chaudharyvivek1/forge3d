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

**Not implemented, and you should know it:** TLS termination, rate limiting, CORS
policy beyond fastmcp's defaults, and request-size limits. Use a reverse proxy
(nginx, Caddy, Traefik) for all four.

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
