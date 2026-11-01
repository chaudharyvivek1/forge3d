# Production reliability

What this server does when things go wrong — and why each behaviour is a
deliberate choice rather than a default inherited from a demo.

## The principle

**Degrade loudly.** An overloaded or partially broken server must say so in a way
the caller can act on. Silent queueing, silent retries and silent 200s are how a
3D pipeline becomes mysterious at 2 a.m.

## Health

`GET /health` (and `/healthz`) needs no token — a load balancer cannot hold one.

```json
{
  "ok": true,
  "service": "forge3d",
  "version": "0.3.0",
  "uptime_s": 1284.3,
  "exec_mode": "subprocess",
  "sandbox": "auto",
  "max_concurrent": 2,
  "jobs": { "recent": 12, "running": 1, "failed": 0 }
}
```

It reports the isolation **actually in force**, so "is this deployment safe?" is
answerable from outside without reading logs. It never includes a token or a key.

## Concurrency

3D work is CPU-bound. Ten concurrent builds on four cores makes every one of them
slow, and a slow build looks like a hung server to a client with a timeout.

- `FORGE3D_MAX_CONCURRENT` (default 2) — generations allowed at once.
- `FORGE3D_QUEUE_WAIT` (default 30 s) — how long a request waits for a free slot.

Past that wait the call fails honestly:

```json
{
  "ok": false,
  "error": "server busy: 2 generations already running",
  "hint": "retry shortly, or raise FORGE3D_MAX_CONCURRENT / FORGE3D_QUEUE_WAIT. ..."
}
```

A short queue absorbs bursts. An unbounded one hides an overload that the client
needs to see — and turns it into a client-side timeout that looks like a server
fault.

Slots are released in a `finally`, so one failed build cannot leave the server
permanently "busy". There is a test for exactly that.

## Rate limiting

`FORGE3D_RATE_LIMIT` (requests/minute, default `0` = off) applies a token bucket
per caller, keyed by bearer token when present and by client address otherwise.
Exceeding it returns `429` with `Retry-After: 5`.

This is a guard against a runaway loop, not a billing-grade quota: it is
per-process and in-memory. Multi-process deployments need a real limiter in the
proxy.

## Retries

Hosted text-to-3D calls retry transient failures with exponential backoff and
jitter (`FORGE3D_HOSTED_RETRIES`, default 3):

- retried: connection errors, timeouts, and **5xx**
- not retried: **4xx** — a bad request is the caller's fault, and retrying it
  just burns the provider quota
- jittered: a fleet of agents must not resynchronise into a thundering herd

## Job durability

- Records are written atomically (temp file + `os.replace`) behind a lock, so a
  crash cannot leave a half-written JSON that later parses as corrupt.
- Each generation writes to `models/<name>/<job-id>/`, so two concurrent runs of
  "chair" cannot overwrite each other.
- `recover_stale()` runs at the start of every generation and marks jobs orphaned
  by a restart as `interrupted` instead of leaving them `running` forever — which
  is what a polling agent would otherwise wait on indefinitely.

## Logs

`FORGE3D_LOG_JSON=1` emits one JSON object per line on stderr:

```json
{"ts": 1793531289.2, "event": "tool_call", "tool": "create_model", "ms": 4213.7, "status": "ok"}
{"ts": 1793531290.1, "event": "http_request", "method": "POST", "path": "/mcp", "status": 200, "ms": 4220.4}
```

Without it, a plain line per HTTP request. Tool calls are logged through an MCP
middleware, so the record exists for stdio clients too — not just HTTP.

## Timeouts, and what "timeout" honestly means

| Layer | Setting | Behaviour on expiry |
|---|---|---|
| model code | `FORGE3D_CODE_TIMEOUT` | `subprocess` mode: child killed. `inprocess` mode: work abandoned, keeps running |
| HTTP request | client-side | server keeps going; results are still written to the job |
| job | `FORGE3D_JOB_TIMEOUT` | hosted polling stops and the job fails |

The `inprocess` caveat is the reason `subprocess` exists and the reason HTTP mode
forces it. Python cannot kill a thread; it can kill a process.

## Startup

Cold import is the slow part: `trimesh` pulls in `scipy.spatial` (~13 s on a slow
container disk, 1.5–3 s warm). MCP clients spawn the server and wait for the
`initialize` handshake, so give a first launch ~30 s. Nothing is hanging — it is
importing a geometry library. Details in [platforms.md](platforms.md).

## What is still not handled

Being explicit, because a reliability page that only lists strengths is
marketing:

- **No distributed state.** Jobs, rate limits and semaphores are per-process.
  Two server instances do not coordinate; give each its own `FORGE3D_WORKDIR` or
  put a real queue in front.
- **No request cancellation from the client.** If a client disconnects mid-build,
  the build finishes. The result is on disk and the job record is accurate.
- **No backpressure on disk.** A runaway stream of generations can fill the
  workdir. `FORGE3D_MAX_FILE_MB` bounds a single child's writes, not the total.
- **No automatic cleanup.** Generated models accumulate. Prune `models/` on a
  schedule; nothing here does it for you.
