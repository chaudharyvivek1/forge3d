"""Production behaviour: health, rate limiting, concurrency, retries, logs.

The theme of this file is that an overloaded or degraded server must *say so*.
Silent queueing, silent retries and silent 200s are how a 3D pipeline becomes
mysterious at 2 a.m.
"""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from forge3d import config, server
from forge3d.http_server import (
    AccessLog,
    HealthApp,
    HttpConfig,
    RateLimitMiddleware,
    build_app,
)
from forge3d.server import mcp

CUBE = "import trimesh\ndef build(): return trimesh.creation.box(extents=[4, 4, 4])"
SLOW = "import time, trimesh\ndef build():\n    time.sleep(2)\n    return trimesh.creation.box()"


# ---------------------------------------------------------------------------
# health endpoint
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(app, port: int):
    import uvicorn

    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=uv.run, daemon=True)
    t.start()
    for _ in range(200):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return uv, t


def _get(port: int, path: str, headers: dict | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_health_needs_no_token_and_reports_isolation():
    """A load balancer cannot hold a bearer token, and an operator needs to know
    what isolation is actually in force."""
    cfg = HttpConfig(host="127.0.0.1", port=_free_port(), token="secret")
    app = build_app(mcp, cfg)
    uv, thread = _start(app, cfg.port)
    try:
        status, body = _get(cfg.port, "/health")
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True and data["service"] == "forge3d"
        assert "version" in data and "exec_mode" in data and "sandbox" in data
        assert data["uptime_s"] >= 0
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


def test_healthz_alias_works():
    cfg = HttpConfig(host="127.0.0.1", port=_free_port())
    uv, thread = _start(build_app(mcp, cfg), cfg.port)
    try:
        assert _get(cfg.port, "/healthz")[0] == 200
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


def test_health_does_not_leak_secrets():
    cfg = HttpConfig(host="127.0.0.1", port=_free_port(), token="super-secret-token")
    uv, thread = _start(build_app(mcp, cfg), cfg.port)
    try:
        _, body = _get(cfg.port, "/health")
        assert "super-secret-token" not in body
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------
async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(instance, path="/mcp", headers=None):
    """Drive one request through an already-constructed middleware instance.

    Constructing a fresh instance per call would reset the rate-limit bucket and
    make the test pass for the wrong reason — which it briefly did.
    """
    sent = []
    scope = {"type": "http", "path": path, "client": ("1.2.3.4", 1234),
             "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(instance(scope, receive, send))
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


def _run_asgi(mw, path="/mcp", headers=None):
    """`mw` is a factory taking an app (used for stateless middlewares)."""
    return _call(mw(_ok_app), path=path, headers=headers)


def test_rate_limiter_allows_then_rejects():
    limiter = RateLimitMiddleware(_ok_app, per_minute=2, burst=2)
    assert _call(limiter) == 200
    assert _call(limiter) == 200
    assert _call(limiter) == 429


def test_rate_limiter_isolates_callers():
    limiter = RateLimitMiddleware(_ok_app, per_minute=1, burst=1)
    assert _call(limiter, headers={"authorization": "Bearer aaa"}) == 200
    assert _call(limiter, headers={"authorization": "Bearer aaa"}) == 429
    # a different token has its own bucket
    assert _call(limiter, headers={"authorization": "Bearer bbb"}) == 200


def test_rate_limiter_leaves_health_alone():
    limiter = RateLimitMiddleware(_ok_app, per_minute=1, burst=1)
    assert _call(limiter, path="/health") == 200
    assert _call(limiter, path="/health") == 200


def test_rate_limiter_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("FORGE3D_RATE_LIMIT", "0")
    config._settings = None
    try:
        assert config.get_settings().rate_limit == 0
    finally:
        config._settings = None


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------
def test_busy_server_says_busy_instead_of_queueing_forever(monkeypatch):
    """Unbounded queueing hides overload behind a client timeout."""
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    monkeypatch.setenv("FORGE3D_QUEUE_WAIT", "1")
    config._settings = None
    server._SLOTS.clear()
    try:
        started = threading.Event()

        def hog():
            started.set()
            server.create_model(name="hog", backend="procedural", source=SLOW,
                                render_previews=False, make_viewer=False)

        t = threading.Thread(target=hog)
        t.start()
        started.wait(2)
        time.sleep(0.2)  # let the hog take the only slot

        result = server.create_model(name="second", backend="procedural", source=CUBE,
                                     render_previews=False, make_viewer=False)
        assert result["ok"] is False
        assert "busy" in result["error"]
        assert "FORGE3D_MAX_CONCURRENT" in result["hint"]
        t.join(timeout=30)
    finally:
        monkeypatch.delenv("FORGE3D_MAX_CONCURRENT", raising=False)
        monkeypatch.delenv("FORGE3D_QUEUE_WAIT", raising=False)
        config._settings = None
        server._SLOTS.clear()


def test_slot_is_released_after_a_failed_generation(monkeypatch):
    """A leaked semaphore turns one bad build into a permanently 'busy' server."""
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    monkeypatch.setenv("FORGE3D_QUEUE_WAIT", "1")
    config._settings = None
    server._SLOTS.clear()
    try:
        first = server.create_model(name="broken", backend="procedural",
                                    source="def build(: pass",
                                    render_previews=False, make_viewer=False)
        assert first["ok"] is False
        second = server.create_model(name="after-failure", backend="procedural", source=CUBE,
                                     render_previews=False, make_viewer=False)
        assert second["ok"] is True, second
    finally:
        monkeypatch.delenv("FORGE3D_MAX_CONCURRENT", raising=False)
        monkeypatch.delenv("FORGE3D_QUEUE_WAIT", raising=False)
        config._settings = None
        server._SLOTS.clear()


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_hosted_request_retries_transient_failures(monkeypatch):
    from forge3d.backends import hosted

    calls = {"n": 0}

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise hosted.requests.RequestException("connection reset")
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(hosted.requests, "request", flaky)
    monkeypatch.setattr(hosted.time, "sleep", lambda *_: None)

    resp = hosted._request("POST", "http://x/generate", attempts=3, json={})
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_hosted_request_gives_up_after_attempts(monkeypatch):
    from forge3d.backends import hosted

    calls = {"n": 0}

    def always_fail(method, url, **kwargs):
        calls["n"] += 1
        raise hosted.requests.RequestException("boom")

    monkeypatch.setattr(hosted.requests, "request", always_fail)
    monkeypatch.setattr(hosted.time, "sleep", lambda *_: None)

    with pytest.raises(hosted.requests.RequestException):
        hosted._request("POST", "http://x/generate", attempts=2, json={})
    assert calls["n"] == 2


def test_hosted_retries_5xx_but_not_4xx(monkeypatch):
    from forge3d.backends import hosted

    monkeypatch.setattr(hosted.time, "sleep", lambda *_: None)

    monkeypatch.setattr(hosted.requests, "request", lambda *a, **k: _Resp(500))
    with pytest.raises(hosted.requests.RequestException):
        hosted._request("POST", "http://x", attempts=2)

    calls = {"n": 0}

    def client_error(*a, **k):
        calls["n"] += 1
        return _Resp(400)

    monkeypatch.setattr(hosted.requests, "request", client_error)
    resp = hosted._request("POST", "http://x", attempts=3)
    assert resp.status_code == 400
    assert calls["n"] == 1, "a 4xx is the caller's fault — retrying wastes quota"


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def test_access_log_writes_one_line_per_request(capsys):
    assert _call(AccessLog(_ok_app)) == 200
    captured = capsys.readouterr()
    assert "/mcp" in captured.err


def test_access_log_emits_json_when_asked(capsys, monkeypatch):
    monkeypatch.setenv("FORGE3D_LOG_JSON", "1")
    config._settings = None
    try:
        assert _call(AccessLog(_ok_app)) == 200
        line = capsys.readouterr().err.strip().splitlines()[-1]
        entry = json.loads(line)
        assert entry["event"] == "http_request"
        assert entry["path"] == "/mcp" and entry["status"] == 200
        assert "ms" in entry
    finally:
        monkeypatch.delenv("FORGE3D_LOG_JSON", raising=False)
        config._settings = None


def test_tool_calls_are_logged_as_json(capsys, monkeypatch):
    monkeypatch.setenv("FORGE3D_LOG_JSON", "1")
    config._settings = None
    try:
        from fastmcp import Client

        async def go():
            async with Client(mcp) as client:
                await client.call_tool("list_backends", {})

        asyncio.run(go())
        lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")]
        events = [json.loads(ln) for ln in lines]
        tool_events = [e for e in events if e.get("event") == "tool_call"]
        assert tool_events, f"no tool_call log line; got {events[:3]}"
        assert tool_events[0]["tool"] == "list_backends"
        assert tool_events[0]["status"] == "ok"
    finally:
        monkeypatch.delenv("FORGE3D_LOG_JSON", raising=False)
        config._settings = None


# ---------------------------------------------------------------------------
# health wrapper plumbing
# ---------------------------------------------------------------------------
def test_health_app_passes_other_paths_through():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 418, "headers": []})
        await send({"type": "http.response.body", "body": b"teapot"})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/mcp", "headers": []}
    asyncio.run(HealthApp(app)(scope, receive, send))
    assert sent[0]["status"] == 418
