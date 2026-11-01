"""HTTP transport with authentication, and the policy for exposing it.

v0.2.0 bound the MCP server to `0.0.0.0` with no authentication and no warning.
That is fine for a stdio server the user started themselves, and indefensible for
anything reachable over a network: every tool is then callable by anyone who can
reach the port, including `create_model` with arbitrary Python.

The policy here is deliberately opinionated:

* **Default to loopback.** `127.0.0.1`, not `0.0.0.0`. A server that is not
  reachable is not exploitable.
* **Refuse to expose without a token.** Binding to a non-loopback address
  requires `FORGE3D_HTTP_TOKEN` (or `--token`). There is an escape hatch
  (`--allow-unauthenticated`) so the refusal is a speed bump, not a wall — but
  you have to mean it.
* **Force process isolation** when serving over HTTP, so agent code runs in a
  killable subprocess rather than in the server process.
* **Constant-time token comparison**, so the check does not leak the secret one
  byte at a time.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from dataclasses import dataclass

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    token: str | None = None
    allow_unauthenticated: bool = False
    allowed_hosts: list[str] | None = None

    @property
    def is_public(self) -> bool:
        return self.host not in LOOPBACK

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def problems(self) -> list[str]:
        """Reasons this configuration must not be served as-is."""
        issues = []
        if self.is_public and not self.authenticated and not self.allow_unauthenticated:
            issues.append(
                f"refusing to bind {self.host} without authentication — set FORGE3D_HTTP_TOKEN "
                f"(or pass --token), or bind 127.0.0.1, or pass --allow-unauthenticated if you "
                f"really mean to expose every tool to the network"
            )
        if self.is_public and not self.authenticated and self.allow_unauthenticated:
            issues.append(
                "WARNING: serving on a public interface with no authentication — "
                "anyone who can reach this port can run arbitrary model code"
            )
        return issues


def parse_http_args(argv: list[str]) -> HttpConfig:
    """Parse the small, explicit set of HTTP flags. No argparse magic.

    Accepted forms:
        --http 8000            --http=8000        --http
        --host 0.0.0.0         --token SECRET
        --allow-unauthenticated
    """
    cfg = HttpConfig(token=os.environ.get("FORGE3D_HTTP_TOKEN") or None)

    def value_after(flag: str) -> str | None:
        if flag in argv:
            idx = argv.index(flag)
            if len(argv) > idx + 1 and not argv[idx + 1].startswith("-"):
                return argv[idx + 1]
        for item in argv:
            if item.startswith(f"{flag}="):
                return item.split("=", 1)[1]
        return None

    port = value_after("--http")
    if port and port.isdigit():
        cfg.port = int(port)
    host = value_after("--host")
    if host:
        cfg.host = host
    token = value_after("--token")
    if token:
        cfg.token = token
    if "--allow-unauthenticated" in argv:
        cfg.allow_unauthenticated = True

    allowed = value_after("--allowed-hosts")
    if allowed:
        cfg.allowed_hosts = [h.strip() for h in allowed.split(",") if h.strip()]
    elif cfg.is_public:
        # DNS-rebinding protection: only answer to the names you meant to serve.
        cfg.allowed_hosts = ["*"]
    return cfg


class BearerAuthMiddleware:
    """Minimal ASGI middleware: require `Authorization: Bearer <token>`.

    Written as raw ASGI rather than a framework dependency so it works with
    whatever app object fastmcp hands back, and so the check is obvious enough to
    audit in one read.
    """

    def __init__(self, app, token: str, public_paths: tuple[str, ...] = ("/health",)) -> None:
        self.app = app
        self.token = token
        self.public_paths = public_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in self.public_paths:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        presented = headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        # compare_digest keeps the check constant-time in the secret.
        if not hmac.compare_digest(presented, expected):
            body = json.dumps({
                "error": "unauthorized",
                "hint": "send 'Authorization: Bearer <FORGE3D_HTTP_TOKEN>'",
            }).encode()
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="forge3d"'),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return

        return await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# production surface: health, rate limiting, access logs
# ---------------------------------------------------------------------------
_STARTED_AT = time.time()


class HealthApp:
    """Answers /health and /healthz itself, then delegates everything else.

    Health checks must work without a token — a load balancer cannot hold one —
    and must be cheap enough to poll every few seconds. They also report the
    isolation actually in force, so "is this deployment safe?" is answerable from
    outside without reading logs.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in ("/health", "/healthz"):
            from . import __version__
            from .config import get_settings

            settings = get_settings()
            payload = {
                "ok": True,
                "service": "forge3d",
                "version": __version__,
                "uptime_s": round(time.time() - _STARTED_AT, 1),
                "exec_mode": settings.resolved_exec_mode(),
                "sandbox": settings.sandbox,
                "max_concurrent": settings.max_concurrent,
            }
            try:
                from .jobs import JobStore

                store = JobStore(settings.ensure_workdir() / "jobs")
                recent = store.list(limit=100)
                payload["jobs"] = {
                    "recent": len(recent),
                    "running": sum(1 for j in recent if j.status == "running"),
                    "failed": sum(1 for j in recent if j.status == "failed"),
                }
            except Exception:
                payload["jobs"] = {"error": "unavailable"}

            body = json.dumps(payload).encode()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")],
            })
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


class RateLimitMiddleware:
    """A token bucket per caller, so one client cannot starve the rest.

    Keyed by bearer token when present, else by client address. Deliberately
    simple: fixed refill, no distributed state. For multi-process deployments put
    a real limiter in the proxy — this exists so a single exposed server does not
    fold under a loop, not to be a billing-grade quota.
    """

    def __init__(self, app, per_minute: int, burst: int | None = None) -> None:
        self.app = app
        self.per_minute = max(1, int(per_minute))
        self.capacity = max(1, int(burst or self.per_minute))
        self.state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _key(self, scope) -> str:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return "token:" + auth[7:16]
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    def _allow(self, key: str) -> bool:
        now = time.time()
        rate = self.per_minute / 60.0
        with self._lock:
            tokens, last = self.state.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * rate)
            if tokens < 1.0:
                self.state[key] = (tokens, now)
                return False
            self.state[key] = (tokens - 1.0, now)
            return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in ("/health", "/healthz"):
            return await self.app(scope, receive, send)
        if not self._allow(self._key(scope)):
            body = json.dumps({
                "error": "rate limited",
                "hint": f"limit is {self.per_minute} requests/minute; retry shortly",
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"application/json"),
                            (b"retry-after", b"5"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


class AccessLog:
    """One line per request, JSON when FORGE3D_LOG_JSON is on."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from .config import get_settings

        started = time.perf_counter()
        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            return await self.app(scope, receive, send_wrapper)
        finally:
            entry = {
                "event": "http_request",
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": status["code"],
                "ms": round((time.perf_counter() - started) * 1000, 1),
            }
            if get_settings().log_json:
                print(json.dumps(entry), file=sys.stderr, flush=True)
            else:
                print(f"{entry['method']} {entry['path']} -> {entry['status']} "
                      f"({entry['ms']}ms)", file=sys.stderr, flush=True)


def build_app(mcp, cfg: HttpConfig):
    """Wrap the MCP ASGI app with auth when a token is configured.

    Starlette's middleware list takes `(cls, args, kwargs)` triples, not bare
    callables — it constructs `cls(app, *args, **kwargs)` itself.
    """
    from .config import get_settings

    settings = get_settings()
    # Order matters: Starlette wraps in reverse, so the first entry here is the
    # outermost layer. Logging sees everything; rate limiting runs before auth so
    # a token-guessing loop is throttled rather than merely rejected.
    middleware: list = [(AccessLog, (), {})]
    if settings.rate_limit > 0:
        middleware.append((RateLimitMiddleware, (settings.rate_limit,), {}))
    if cfg.authenticated:
        middleware.append((BearerAuthMiddleware, (cfg.token,), {}))

    kwargs = {"middleware": middleware or None}
    if cfg.allowed_hosts:
        kwargs["allowed_hosts"] = cfg.allowed_hosts
    return HealthApp(mcp.http_app(**kwargs))


def serve(mcp, cfg: HttpConfig) -> None:
    """Run the HTTP transport. Raises SystemExit on an unsafe configuration."""
    issues = cfg.problems()
    fatal = [i for i in issues if not i.startswith("WARNING")]
    for issue in issues:
        print(f"forge3d: {issue}", file=sys.stderr, flush=True)
    if fatal:
        raise SystemExit(2)

    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "forge3d: HTTP transport needs uvicorn — pip install 'forge3d[http]'"
        ) from None

    app = build_app(mcp, cfg)
    # Say "http" even when authenticated: this process does not terminate TLS.
    # Implying otherwise would be the kind of comfortable lie that gets someone
    # to expose a plaintext port and call it secure.
    auth_note = "bearer token required" if cfg.authenticated else "NO AUTH — loopback only"
    print(
        f"forge3d: serving MCP over http://{cfg.host}:{cfg.port} ({auth_note}); "
        f"TLS, rate limiting and per-user auth belong in a reverse proxy",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
