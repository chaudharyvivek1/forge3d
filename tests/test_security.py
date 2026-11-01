"""Security and isolation behaviour — the fixes for the v0.2.0 audit findings.

These tests exist because the previous release bound the HTTP transport to
0.0.0.0 with no authentication, and ran agent code in-process with no way to
cancel it. Both are now covered by assertions rather than by good intentions.
"""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from forge3d import config, jobs, server
from forge3d.http_server import BearerAuthMiddleware, HttpConfig, build_app, parse_http_args
from forge3d.server import mcp

CUBE = "import trimesh\ndef build(): return trimesh.creation.box(extents=[4, 4, 4])"
RUNAWAY = "import trimesh\ndef build():\n    while True:\n        pass"


# ---------------------------------------------------------------------------
# bind policy
# ---------------------------------------------------------------------------
def test_http_defaults_to_loopback():
    """0.0.0.0 by default was the actual bug: every tool reachable by anyone."""
    cfg = parse_http_args(["--http", "8000"])
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.is_public is False


def test_public_bind_without_token_is_refused():
    cfg = parse_http_args(["--http", "8000", "--host", "0.0.0.0"])
    assert cfg.is_public is True
    assert cfg.authenticated is False
    assert any("refusing" in p for p in cfg.problems())


def test_public_bind_with_token_is_allowed():
    cfg = parse_http_args(["--http", "8000", "--host", "0.0.0.0", "--token", "s3cret"])
    assert cfg.authenticated is True
    assert [p for p in cfg.problems() if not p.startswith("WARNING")] == []


def test_public_bind_with_explicit_opt_out_warns_loudly():
    cfg = parse_http_args(["--http", "8000", "--host", "0.0.0.0", "--allow-unauthenticated"])
    problems = cfg.problems()
    assert any(p.startswith("WARNING") for p in problems)
    assert not [p for p in problems if not p.startswith("WARNING")]


def test_token_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("FORGE3D_HTTP_TOKEN", "envtoken")
    cfg = parse_http_args(["--http", "9000"])
    assert cfg.token == "envtoken"


def test_flag_token_beats_env_token(monkeypatch):
    monkeypatch.setenv("FORGE3D_HTTP_TOKEN", "envtoken")
    cfg = parse_http_args(["--http", "9000", "--token", "flagtoken"])
    assert cfg.token == "flagtoken"


def test_http_flag_forms():
    assert parse_http_args(["--http"]).port == 8000
    assert parse_http_args(["--http=8123"]).port == 8123
    assert parse_http_args(["--http", "8124"]).port == 8124


def test_loopback_variants_are_not_public():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert HttpConfig(host=host).is_public is False


# ---------------------------------------------------------------------------
# bearer middleware
# ---------------------------------------------------------------------------
def _run_asgi(middleware, headers, path="/mcp"):
    """Drive one ASGI request through `middleware` and capture what it sent."""
    sent = []
    reached_app = {"called": False}

    async def app(scope, receive, send):
        reached_app["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = middleware(app)
    scope = {
        "type": "http",
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(wrapped(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, reached_app["called"]


def test_middleware_rejects_missing_token():
    status, reached = _run_asgi(lambda app: BearerAuthMiddleware(app, "abc"), {})
    assert status == 401
    assert reached is False


def test_middleware_rejects_wrong_token():
    status, reached = _run_asgi(
        lambda app: BearerAuthMiddleware(app, "abc"), {"authorization": "Bearer nope"}
    )
    assert status == 401 and reached is False


def test_middleware_accepts_correct_token():
    status, reached = _run_asgi(
        lambda app: BearerAuthMiddleware(app, "abc"), {"authorization": "Bearer abc"}
    )
    assert status == 200 and reached is True


def test_middleware_leaves_health_open():
    status, reached = _run_asgi(lambda app: BearerAuthMiddleware(app, "abc"), {}, path="/health")
    assert status == 200 and reached is True


def test_middleware_401_body_explains_the_fix():
    sent = []

    async def app(scope, receive, send):
        raise AssertionError("must not be reached")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/mcp", "headers": []}
    asyncio.run(BearerAuthMiddleware(app, "abc")(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"Authorization: Bearer" in body


# ---------------------------------------------------------------------------
# live HTTP server
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_http(cfg: HttpConfig):
    import uvicorn

    app = build_app(mcp, cfg)
    uv_config = uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="error")
    uv_server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    for _ in range(200):
        try:
            with socket.create_connection((cfg.host, cfg.port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return uv_server, thread


def _post(port: int, headers: dict) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        headers={"content-type": "application/json", "accept": "application/json, text/event-stream", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_live_server_rejects_unauthenticated_requests():
    cfg = HttpConfig(host="127.0.0.1", port=_free_port(), token="test-token")
    uv_server, thread = _start_http(cfg)
    try:
        assert _post(cfg.port, {}) == 401
        assert _post(cfg.port, {"authorization": "Bearer wrong"}) == 401
    finally:
        uv_server.should_exit = True
        thread.join(timeout=10)


def test_live_server_accepts_the_configured_token():
    cfg = HttpConfig(host="127.0.0.1", port=_free_port(), token="test-token")
    uv_server, thread = _start_http(cfg)
    try:
        status = _post(cfg.port, {"authorization": "Bearer test-token"})
        assert status != 401, "authenticated request must get past the middleware"
    finally:
        uv_server.should_exit = True
        thread.join(timeout=10)


def test_serve_refuses_an_unsafe_configuration():
    from forge3d.http_server import serve

    cfg = HttpConfig(host="0.0.0.0", port=_free_port())
    with pytest.raises(SystemExit):
        serve(mcp, cfg)


# ---------------------------------------------------------------------------
# code execution isolation
# ---------------------------------------------------------------------------
def test_subprocess_mode_builds_a_model(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE3D_EXEC_MODE", "subprocess")
    config._settings = None
    try:
        result = server.create_model(
            name="subproc", backend="procedural", source=CUBE,
            render_previews=False, make_viewer=False,
        )
        assert result["ok"], result
        from pathlib import Path

        assert Path(result["path"]).exists()
    finally:
        monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
        config._settings = None


def test_subprocess_mode_really_kills_runaway_code(tmp_path, monkeypatch):
    """The finding: a timed-out build kept burning CPU forever.

    A process boundary means the parent can actually kill it. This asserts the
    call returns, and returns promptly.
    """
    monkeypatch.setenv("FORGE3D_EXEC_MODE", "subprocess")
    monkeypatch.setenv("FORGE3D_CODE_TIMEOUT", "3")
    config._settings = None
    try:
        started = time.time()
        result = server.create_model(
            name="runaway", backend="procedural", source=RUNAWAY,
            render_previews=False, make_viewer=False,
        )
        elapsed = time.time() - started
        assert result["ok"] is False
        assert "killed" in result["error"] or "exceeded" in result["error"]
        assert elapsed < 30, f"should have been killed quickly, took {elapsed:.1f}s"
    finally:
        monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
        monkeypatch.delenv("FORGE3D_CODE_TIMEOUT", raising=False)
        config._settings = None


def test_subprocess_mode_reports_syntax_errors_with_a_log(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE3D_EXEC_MODE", "subprocess")
    config._settings = None
    try:
        result = server.create_model(name="bad-syntax", backend="procedural",
                                     source="def build(: pass",
                                     render_previews=False, make_viewer=False)
        assert result["ok"] is False
        assert result["log"], "the agent needs the traceback to self-correct"
    finally:
        monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
        config._settings = None


def test_code_exec_flag_still_blocks_both_modes(monkeypatch):
    monkeypatch.setenv("FORGE3D_ALLOW_CODE_EXEC", "false")
    config._settings = None
    try:
        for mode in ("inprocess", "subprocess"):
            monkeypatch.setenv("FORGE3D_EXEC_MODE", mode)
            config._settings = None
            result = server.create_model(name=f"blocked-{mode}", backend="procedural",
                                         source=CUBE, render_previews=False, make_viewer=False)
            assert result["ok"] is False
            assert "disabled" in result["error"]
    finally:
        monkeypatch.delenv("FORGE3D_ALLOW_CODE_EXEC", raising=False)
        monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
        config._settings = None


def test_auto_mode_picks_subprocess_for_http(monkeypatch):
    monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
    monkeypatch.setenv("FORGE3D_TRANSPORT", "http")
    config._settings = None
    try:
        assert config.get_settings().resolved_exec_mode() == "subprocess"
    finally:
        monkeypatch.delenv("FORGE3D_TRANSPORT", raising=False)
        config._settings = None


def test_auto_mode_stays_inprocess_for_stdio(monkeypatch):
    monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
    monkeypatch.delenv("FORGE3D_TRANSPORT", raising=False)
    config._settings = None
    assert config.get_settings().resolved_exec_mode() == "inprocess"
    config._settings = None


# ---------------------------------------------------------------------------
# job isolation and recovery
# ---------------------------------------------------------------------------
def test_concurrent_generations_of_the_same_name_do_not_collide():
    """Two agents building "chair" used to write the same path and clobber."""
    a = server.create_model(name="same-name", backend="procedural", source=CUBE,
                            render_previews=False, make_viewer=False)
    b = server.create_model(name="same-name", backend="procedural", source=CUBE,
                            render_previews=False, make_viewer=False)
    assert a["ok"] and b["ok"]
    assert a["path"] != b["path"]
    assert a["job_id"] in a["path"] and b["job_id"] in b["path"]


def test_job_records_are_written_atomically(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "atomic"})
    store.save(job)
    on_disk = json.loads((tmp_path / "jobs" / f"{job.id}.json").read_text(encoding="utf-8"))
    assert on_disk["id"] == job.id
    # no temp files left behind
    assert [p.suffix for p in (tmp_path / "jobs").iterdir()] == [".json"]


def test_stale_jobs_are_recovered_not_left_running(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "crashed"})
    job.status = jobs.RUNNING
    store.save(job)

    # Simulate a process that died mid-generation: the record on disk still says
    # "running" and its timestamp is old. save() would refresh updated_at, so
    # write the stale record directly.
    record_path = tmp_path / "jobs" / f"{job.id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["updated_at"] = time.time() - 10_000
    record_path.write_text(json.dumps(record), encoding="utf-8")

    recovered = store.recover_stale(max_age_seconds=60)
    assert job.id in recovered
    again = store.get(job.id)
    assert again.status == jobs.FAILED
    assert "interrupted" in again.error


def test_recent_running_jobs_are_not_touched(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "in-flight"})
    job.status = jobs.RUNNING
    store.save(job)

    assert store.recover_stale(max_age_seconds=60) == []
    assert store.get(job.id).status == jobs.RUNNING


def test_concurrent_job_writes_do_not_corrupt_records(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "racy"})

    def hammer():
        for _ in range(25):
            store.save(job)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((tmp_path / "jobs" / f"{job.id}.json").read_text(encoding="utf-8"))
    assert data["id"] == job.id
