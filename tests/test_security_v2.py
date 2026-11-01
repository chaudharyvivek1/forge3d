"""v0.6.2 security fixes: central exec policy, SSRF guard, idempotent submission.

Each test here maps to a finding from the external review, and each would have
failed against v0.6.1.
"""

import io
import json
import time

import pytest
import requests

from forge3d import backends, config, server
from forge3d.backends.hosted import HostedBackend
from forge3d.jobs import RUNNING, JobStore
from forge3d.netguard import UnsafeURLError, check_url, fetch_bytes, fetch_image
from forge3d.providers import registry
from forge3d.providers.base import (
    SUCCEEDED,
    AmbiguousSubmission,
    GenerationProvider,
    GenerationRequest,
    ProviderError,
    ProviderJob,
    request_with_retries,
)
from forge3d.spec import ModelSpec

CUBE = "import trimesh\ndef build(): return trimesh.creation.box()"


# =========================================================================
# Finding 1 — the execution policy is central, not per-backend
# =========================================================================
def test_every_code_backend_declares_it():
    entries = {b["name"]: b for b in backends.list_backends()}
    for name in ("procedural", "cadquery", "blender"):
        assert entries[name]["executes_user_code"] is True, name
    for name in ("image", "openscad", "hosted"):
        assert entries[name]["executes_user_code"] is False, name


@pytest.mark.parametrize("backend_name", ["procedural", "cadquery", "blender"])
def test_disabling_execution_blocks_every_code_backend(monkeypatch, backend_name):
    """v0.6.1 only enforced this in the procedural backend."""
    monkeypatch.setenv("FORGE3D_ALLOW_CODE_EXEC", "false")
    config._settings = None
    try:
        _name, result = backends.run(backend_name, ModelSpec(name="t"), "/tmp/policy",
                                     source="x = 1")
        assert result.ok is False
        assert "code execution is disabled" in result.error
        assert backend_name in result.error
    finally:
        monkeypatch.delenv("FORGE3D_ALLOW_CODE_EXEC", raising=False)
        config._settings = None


def test_disabling_execution_leaves_geometry_backends_alone(monkeypatch):
    """Turning off code execution must not turn off the shape library."""
    monkeypatch.setenv("FORGE3D_ALLOW_CODE_EXEC", "false")
    config._settings = None
    try:
        out = server.create_shape(shape="twisted_prism", resolution=24, name="policy-shape",
                                  render_previews=False, make_viewer=False)
        assert out["ok"] is True
    finally:
        monkeypatch.delenv("FORGE3D_ALLOW_CODE_EXEC", raising=False)
        config._settings = None


def test_cadquery_honours_subprocess_mode(monkeypatch):
    """The one code path that ignored FORGE3D_EXEC_MODE entirely."""
    from forge3d.backends.cadquery_backend import CadQueryBackend

    monkeypatch.setenv("FORGE3D_EXEC_MODE", "subprocess")
    config._settings = None
    try:
        backend = CadQueryBackend()
        if not backend.available():
            pytest.skip("cadquery not installed")
        result = backend.generate(ModelSpec(name="cq"), "/tmp/cq-mode",
                                  source="import cadquery as cq\nresult = cq.Workplane('XY').box(1,1,1)")
        assert result.meta.get("mode") in ("subprocess", "inprocess")
    finally:
        monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
        config._settings = None


# =========================================================================
# Finding 2 — SSRF and resource exhaustion through image URLs
# =========================================================================
@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1/a.png", "loopback"),
    ("http://localhost/a.png", "loopback"),
    ("http://169.254.169.254/latest/meta-data/", "link-local"),
    ("http://10.0.0.5/a.png", "private"),
    ("http://192.168.1.1/a.png", "private"),
    ("http://[::1]/a.png", "loopback"),
])
def test_private_and_loopback_addresses_are_refused(url, reason):
    with pytest.raises(UnsafeURLError, match=reason):
        check_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/a.png", "gopher://x/"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(UnsafeURLError, match="not allowed"):
        check_url(url)


class _Resp:
    def __init__(self, status=200, headers=None, body=b"", redirect_to=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.is_redirect = redirect_to is not None
        if redirect_to:
            self.headers["location"] = redirect_to
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def json(self):
        return json.loads(self._body or b"{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_stream(monkeypatch, resp):
    """Patch the transport, not `requests.get`.

    fetch_bytes now pins the connection to a validated IP (urllib3) unless a proxy
    is configured, so patching `requests.get` would only exercise one of the two
    transports — and CI has no proxy.
    """
    monkeypatch.setattr("forge3d.netguard.open_stream",
                        lambda url, **kw: (resp, False))
    return resp


def test_a_redirect_to_localhost_is_refused(monkeypatch):
    """The classic bypass: a public host that 302s to 127.0.0.1.

    The URL uses a literal public address rather than a hostname so the test does
    not need DNS — the sandbox has none, and resolution failure would mask the
    redirect check being verified.
    """
    _fake_stream(monkeypatch, _Resp(302, redirect_to="http://127.0.0.1/secret"))
    with pytest.raises(UnsafeURLError, match="loopback"):
        fetch_bytes("http://93.184.216.34/a.png")


def test_a_declared_oversize_body_is_refused(monkeypatch):
    _fake_stream(monkeypatch, _Resp(200, {"content-type": "image/png"},
                                                body=b"x" * 5000))
    with pytest.raises(UnsafeURLError, match="exceeds|exceeded"):
        fetch_bytes("http://93.184.216.34/a.png", max_bytes=1024)


def test_a_streamed_oversize_body_is_cut_off(monkeypatch):
    """No Content-Length, so the cap has to be enforced while reading."""
    _fake_stream(monkeypatch, _Resp(200, {"content-type": "image/png"},
                                                body=b"x" * 200_000))
    with pytest.raises(UnsafeURLError, match="exceeded"):
        fetch_bytes("http://93.184.216.34/a.png", max_bytes=1000)


def test_a_non_image_content_type_is_refused(monkeypatch):
    _fake_stream(monkeypatch, _Resp(200, {"content-type": "text/html"},
                                                body=b"<html>"))
    with pytest.raises(UnsafeURLError, match="not an accepted image type"):
        fetch_bytes("http://93.184.216.34/a.png")


def test_bytes_that_are_not_an_image_are_refused(monkeypatch, tmp_path):
    _fake_stream(monkeypatch, _Resp(200, {"content-type": "image/png"},
                                                body=b"definitely not a png"))
    with pytest.raises(UnsafeURLError, match="not a decodable image"):
        fetch_image("http://93.184.216.34/a.png", tmp_path / "a.png")


def test_a_decompression_bomb_is_refused(monkeypatch, tmp_path):
    """A tiny file that decodes to an enormous bitmap."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (4000, 4000)).save(buf, format="PNG")
    _fake_stream(monkeypatch, _Resp(200, {"content-type": "image/png"},
                                                body=buf.getvalue()))
    with pytest.raises(UnsafeURLError, match="pixel budget"):
        fetch_image("http://93.184.216.34/a.png", tmp_path / "a.png", max_pixels=1_000_000)


def test_the_image_backend_surfaces_a_refusal(tmp_path):
    result = backends.get_backend("image").generate(
        ModelSpec(name="ssrf", references=["http://127.0.0.1/a.png"]), tmp_path)
    assert result.ok is False
    assert "refused" in result.error


# =========================================================================
# Finding 3 — retries must not duplicate a paid generation
# =========================================================================
def test_non_idempotent_post_is_not_retried_after_a_timeout(monkeypatch):
    calls = {"n": 0}

    def timeout(method, url, **kw):
        calls["n"] += 1
        raise requests.Timeout("read timed out")

    monkeypatch.setattr("forge3d.providers.base.requests.request", timeout)
    monkeypatch.setattr("forge3d.providers.base.time.sleep", lambda *_: None)

    with pytest.raises(requests.Timeout):
        request_with_retries("POST", "http://x", attempts=3, idempotent=False, json={})
    assert calls["n"] == 1, "an ambiguous POST must not be retried"


def test_non_idempotent_post_is_not_retried_on_5xx(monkeypatch):
    calls = {"n": 0}

    def server_error(method, url, **kw):
        calls["n"] += 1
        return _Resp(503)

    monkeypatch.setattr("forge3d.providers.base.requests.request", server_error)
    with pytest.raises(ProviderError, match="may already have been accepted"):
        request_with_retries("POST", "http://x", attempts=3, idempotent=False, json={})
    assert calls["n"] == 1


def test_idempotent_get_is_still_retried(monkeypatch):
    calls = {"n": 0}

    def flaky(method, url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("reset")
        return _Resp(200)

    monkeypatch.setattr("forge3d.providers.base.requests.request", flaky)
    monkeypatch.setattr("forge3d.providers.base.time.sleep", lambda *_: None)
    assert request_with_retries("GET", "http://x", attempts=3).status_code == 200
    assert calls["n"] == 3


def test_submit_request_sends_an_idempotency_key(monkeypatch):
    from forge3d.providers.meshy import MeshyProvider

    seen = {}

    def capture(method, url, **kw):
        seen.update(kw)
        return _Resp(200, body=json.dumps({"result": "job-1"}).encode())

    monkeypatch.setattr("forge3d.providers.base.requests.request", capture)
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_x")
    MeshyProvider().submit(GenerationRequest(prompt="x"))
    assert "Idempotency-Key" in seen["headers"]


def test_an_ambiguous_submission_says_do_not_retry(monkeypatch):
    from forge3d.providers.meshy import MeshyProvider

    def timeout(method, url, **kw):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr("forge3d.providers.base.requests.request", timeout)
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_x")
    with pytest.raises(AmbiguousSubmission) as exc:
        MeshyProvider().submit(GenerationRequest(prompt="x"))
    assert "Do NOT retry" in str(exc.value)
    assert exc.value.idempotency_key


# =========================================================================
# Finding 5 — availability means the *selected* provider
# =========================================================================
def test_availability_follows_the_selected_provider(monkeypatch):
    """Tripo being configured must not make Meshy look available."""
    monkeypatch.setenv("FORGE3D_PROVIDER", "meshy")
    monkeypatch.delenv("FORGE3D_MESHY_KEY", raising=False)
    monkeypatch.setenv("FORGE3D_TRIPO_KEY", "tsk_x")
    config._settings = None
    try:
        backend = HostedBackend()
        assert backend.available() is False
        info = backend.describe()
        assert info["selected_provider"] == "meshy"
        assert info["selected_available"] is False
    finally:
        for var in ("FORGE3D_PROVIDER", "FORGE3D_TRIPO_KEY"):
            monkeypatch.delenv(var, raising=False)
        config._settings = None


# =========================================================================
# Finding 6 — a timed-out provider job gets cancelled
# =========================================================================
class _SlowProvider(GenerationProvider):
    name = "slow"
    verified = True
    key_env = None

    def __init__(self):
        self.cancelled = []

    def available(self):
        return True

    def submit(self, request):
        return ProviderJob(provider=self.name, job_id="slow-1", status="running")

    def get_status(self, job_id):
        return ProviderJob(provider=self.name, job_id=job_id, status="running", progress=0.1)

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return True


def test_timeout_cancels_the_remote_job(monkeypatch, tmp_path):
    provider = _SlowProvider()
    registry.register(provider)
    monkeypatch.setenv("FORGE3D_PROVIDER", "slow")
    monkeypatch.setenv("FORGE3D_JOB_TIMEOUT", "1")
    config._settings = None
    try:
        result = HostedBackend().generate(ModelSpec(name="slow", prompt="x"), tmp_path)
        assert result.ok is False
        assert provider.cancelled == ["slow-1"], "the remote job was left running"
        assert result.meta["cancelled"] is True
        assert "cancelled" in result.error
    finally:
        for var in ("FORGE3D_PROVIDER", "FORGE3D_JOB_TIMEOUT"):
            monkeypatch.delenv(var, raising=False)
        registry._REGISTRY.pop("slow", None)
        config._settings = None


# =========================================================================
# P2 — job heartbeat, process-wide lock, download cap, host allowlist
# =========================================================================
def test_heartbeat_stops_a_live_job_being_recovered(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "long"})
    job.status = RUNNING
    store.save(job)

    other = JobStore(tmp_path / "jobs")     # a second tool call builds a new store
    with store.heartbeat(job.id, interval=0.05):
        # Wait for evidence of a touch instead of assuming the thread was
        # scheduled within a fixed sleep — that assumption failed on a loaded
        # macOS runner, where 0.25 s was not enough for a 0.05 s interval.
        start = job.updated_at
        deadline = time.time() + 10
        while time.time() < deadline and store.get(job.id).updated_at == start:
            time.sleep(0.01)
        assert store.get(job.id).updated_at > start, "the heartbeat never ran"

        assert other.recover_stale(max_age_seconds=0.15) == []
    assert store.get(job.id).status == RUNNING


def test_job_lock_is_shared_between_instances(tmp_path):
    from forge3d import jobs as jobs_module

    a = JobStore(tmp_path / "a")
    b = JobStore(tmp_path / "b")
    assert a._lock is b._lock is jobs_module._PROCESS_LOCK


def test_provider_download_is_size_capped(monkeypatch, tmp_path):
    class Big(GenerationProvider):
        name = "big"
        key_env = None

        def submit(self, request):
            raise NotImplementedError

        def get_status(self, job_id):
            raise NotImplementedError

    monkeypatch.setenv("FORGE3D_MAX_DOWNLOAD_MB", "0")   # 0 MB -> any byte exceeds
    _fake_stream(monkeypatch, _Resp(200, body=b"x" * 5000))
    job = ProviderJob(provider="big", job_id="1", status=SUCCEEDED,
                      asset_url="http://93.184.216.34/x.glb")
    with pytest.raises(ProviderError, match="exceeded"):
        Big().download(job, tmp_path, "x")
    assert not (tmp_path / "x.glb").exists(), "a rejected download must not be left behind"


def test_public_bind_does_not_default_to_a_wildcard_host(monkeypatch):
    from forge3d.http_server import parse_http_args

    monkeypatch.delenv("FORGE3D_HTTP_ALLOWED_HOSTS", raising=False)
    cfg = parse_http_args(["--http", "8000", "--host", "0.0.0.0", "--token", "t"])
    assert cfg.allowed_hosts != ["*"]

    monkeypatch.setenv("FORGE3D_HTTP_ALLOWED_HOSTS", "forge.example.com")
    cfg = parse_http_args(["--http", "8000", "--host", "0.0.0.0", "--token", "t"])
    assert cfg.allowed_hosts == ["forge.example.com"]
