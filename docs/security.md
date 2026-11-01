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
