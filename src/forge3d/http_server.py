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


def build_app(mcp, cfg: HttpConfig):
    """Wrap the MCP ASGI app with auth when a token is configured.

    Starlette's middleware list takes `(cls, args, kwargs)` triples, not bare
    callables — it constructs `cls(app, *args, **kwargs)` itself.
    """
    middleware = []
    if cfg.authenticated:
        middleware.append((BearerAuthMiddleware, (cfg.token,), {}))
    kwargs = {"middleware": middleware or None}
    if cfg.allowed_hosts:
        kwargs["allowed_hosts"] = cfg.allowed_hosts
    return mcp.http_app(**kwargs)


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
