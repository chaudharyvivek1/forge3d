"""v0.6.3 hardening regressions.

Each test corresponds to a gap the v0.6.2 tests could not catch: DNS rebinding
between validation and connection, non-global address ranges that are neither
private nor reserved, the second SSRF path (provider downloads), and the
uncertain-submission state being dropped before it reached the MCP caller.
"""

import json
import socket
import time

import pytest

from forge3d import backends, config, netguard, server
from forge3d.jobs import SUCCEEDED, UNCERTAIN, JobStore
from forge3d.providers import registry
from forge3d.providers.base import (
    AmbiguousSubmission,
    GenerationProvider,
    GenerationRequest,
    ProviderError,
    ProviderJob,
)
from forge3d.spec import ModelSpec


class _Stream:
    def __init__(self, status=200, headers=None, body=b"", ip=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.ip = ip
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def json(self):
        return json.loads(self._body or b"{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


def _public_dns(monkeypatch, address="93.184.216.34"):
    """Make every hostname resolve to one public address."""
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr("forge3d.netguard.socket.getaddrinfo", fake_getaddrinfo)
    return address


# =========================================================================
# Finding 1 — DNS rebinding, and non-global address ranges
# =========================================================================
def test_the_connection_is_pinned_to_the_validated_ip(monkeypatch):
    """Checking a hostname then handing the hostname to the client is TOCTOU.

    The fix is to dial the validated address, keeping the hostname only for TLS.
    """
    ip = _public_dns(monkeypatch)
    seen = {}

    def fake_pinned(url, pinned_ip, timeout, headers):
        seen["ip"] = pinned_ip
        seen["url"] = url
        return _Stream(200, {"content-type": "image/png"}, b"\x89PNG")

    monkeypatch.setattr("forge3d.netguard._open_pinned", fake_pinned)
    monkeypatch.setattr("forge3d.netguard._proxy_configured", lambda: False)

    netguard.fetch_bytes("http://rebind.example.com/a.png")
    assert seen["ip"] == ip, "connected to something other than the validated address"
    assert "rebind.example.com" in seen["url"], "the hostname must survive for TLS/Host"


def test_a_rebinding_host_cannot_change_the_address_after_the_check(monkeypatch):
    """Second resolution returns a private IP; the connection must not follow it."""
    calls = {"n": 0}

    def rebinding_getaddrinfo(host, port, *a, **kw):
        calls["n"] += 1
        address = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr("forge3d.netguard.socket.getaddrinfo", rebinding_getaddrinfo)
    monkeypatch.setattr("forge3d.netguard._proxy_configured", lambda: False)
    dialled = []
    monkeypatch.setattr(
        "forge3d.netguard._open_pinned",
        lambda url, ip, timeout, headers: dialled.append(ip) or
        _Stream(200, {"content-type": "image/png"}, b"x"))

    # Two resolutions happen (validation, then the connection's own check). If the
    # second disagrees, the correct behaviour is to refuse — fail closed — rather
    # than dial an address that was never validated.
    with pytest.raises(netguard.UnsafeURLError):
        netguard.fetch_bytes("http://rebind.example.com/a.png")
    assert dialled == [], "a private address must never be dialled"


@pytest.mark.parametrize("address", [
    "100.64.0.1",       # carrier-grade NAT: not private, not reserved, not global
    "192.0.0.1",        # IETF protocol assignments
    "198.18.0.1",       # benchmarking
    "0.0.0.0",          # unspecified
    "255.255.255.255",  # broadcast
])
def test_non_global_ranges_are_refused(monkeypatch, address):
    """The old blocklist enumerated bad ranges; the rule is now 'must be global'."""
    monkeypatch.setattr(
        "forge3d.netguard.socket.getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))])
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url("http://example.com/a.png")


def test_a_globally_routable_address_is_allowed(monkeypatch):
    _public_dns(monkeypatch)
    netguard.check_url("http://example.com/a.png")   # must not raise


def test_proxy_mode_is_declared_rather_than_silent(monkeypatch):
    """A proxy resolves destinations itself, so pinning cannot apply — say so."""
    _public_dns(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setattr("forge3d.netguard._open_via_proxy",
                        lambda url, timeout, headers: _Stream(
                            200, {"content-type": "image/png"}, b"x"))
    result = netguard.fetch_bytes("http://example.com/a.png")
    assert result.proxied is True
    monkeypatch.delenv("HTTPS_PROXY", raising=False)


def test_proxy_fetch_can_be_refused_outright(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("FORGE3D_ALLOW_PROXY_FETCH", "false")
    with pytest.raises(netguard.UnsafeURLError, match="proxy"):
        netguard.open_stream("http://example.com/a.png")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("FORGE3D_ALLOW_PROXY_FETCH", raising=False)


# =========================================================================
# Finding 2 — provider asset downloads are a second SSRF path
# =========================================================================
class _Downloader(GenerationProvider):
    name = "dl"
    key_env = None

    def submit(self, request):
        raise NotImplementedError

    def get_status(self, job_id):
        raise NotImplementedError


def _job(url):
    return ProviderJob(provider="dl", job_id="1", status=SUCCEEDED, asset_url=url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9200/private",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/asset.glb",
])
def test_provider_downloads_refuse_internal_destinations(monkeypatch, tmp_path, url):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    with pytest.raises(ProviderError, match="refused to download"):
        _Downloader().download(_job(url), tmp_path, "x")


def test_a_redirect_to_a_private_host_is_refused_on_download(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    monkeypatch.setattr("forge3d.netguard._proxy_configured", lambda: False)
    monkeypatch.setattr("forge3d.netguard._open_pinned",
                        lambda url, ip, timeout, headers: _Stream(
                            302, {"location": "http://127.0.0.1/secret"}))
    with pytest.raises(ProviderError, match="refused to download"):
        _Downloader().download(_job("http://93.184.216.34/a.glb"), tmp_path, "x")


def test_a_trusted_host_allowlist_restricts_downloads(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE3D_TRUSTED_DOWNLOAD_HOSTS", "cdn.example.com")
    with pytest.raises(ProviderError, match="not in FORGE3D_TRUSTED_DOWNLOAD_HOSTS"):
        _Downloader().download(_job("http://evil.test/a.glb"), tmp_path, "x")
    monkeypatch.delenv("FORGE3D_TRUSTED_DOWNLOAD_HOSTS", raising=False)


# =========================================================================
# Finding 3 — uncertain submissions must reach the MCP caller
# =========================================================================
def test_a_5xx_on_submit_is_ambiguous_not_a_plain_failure(monkeypatch):
    """502 after the provider may have created the job is not a clean failure."""
    from forge3d.providers.meshy import MeshyProvider

    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_x")
    monkeypatch.setattr("forge3d.providers.base.requests.request",
                        lambda *a, **kw: _Stream(502))
    with pytest.raises(AmbiguousSubmission) as exc:
        MeshyProvider().submit(GenerationRequest(prompt="x"))
    assert exc.value.idempotency_key


class _Ambiguous(GenerationProvider):
    name = "ambiguous"
    key_env = None
    verified = True

    def available(self):
        return True

    def submit(self, request):
        raise AmbiguousSubmission(self.name, "key-abc", "read timeout")

    def get_status(self, job_id):
        raise NotImplementedError


def test_create_model_reports_uncertain_and_does_not_say_failed(monkeypatch):
    registry.register(_Ambiguous())
    monkeypatch.setenv("FORGE3D_PROVIDER", "ambiguous")
    config._settings = None
    try:
        out = server.create_model(prompt="a teapot", backend="hosted", mode="cloud",
                                  name="uncertain-1", idempotency_key="key-abc",
                                  render_previews=False, make_viewer=False)
        assert out["ok"] is False
        assert out.get("uncertain") is True
        assert out["status"] == UNCERTAIN
        assert out["idempotency_key"] == "key-abc"
        assert "Do NOT resubmit" in out["hint"]

        job = server.get_job(out["job_id"])["job"]
        assert job["status"] == UNCERTAIN, "the MCP layer downgraded it to failed"
        assert job["meta"]["idempotency_key"] == "key-abc"
    finally:
        monkeypatch.delenv("FORGE3D_PROVIDER", raising=False)
        registry._REGISTRY.pop("ambiguous", None)
        config._settings = None


def test_a_caller_supplied_key_is_forwarded_to_the_provider(monkeypatch):
    from forge3d.providers.meshy import MeshyProvider

    seen = {}
    monkeypatch.setattr("forge3d.providers.base.requests.request",
                        lambda method, url, **kw: seen.update(kw) or _Stream(
                            200, body=json.dumps({"result": "j1"}).encode()))
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_x")
    MeshyProvider().submit(GenerationRequest(prompt="x", extra={"idempotency_key": "my-key"}))
    assert seen["headers"]["Idempotency-Key"] == "my-key"


# =========================================================================
# Finding 4 — Blender must apply limits or say it did not
# =========================================================================
def test_run_applies_real_limits_on_posix():
    from forge3d.platform_support import IS_WINDOWS, run

    result = run(["python3", "-c", "print(1)"], limits=True, timeout=30)
    if IS_WINDOWS:
        assert result.limits_applied is False, "Windows has no rlimits"
    else:
        assert result.limits_applied is True


def test_blender_uses_the_configured_timeout_and_reports_isolation(monkeypatch, tmp_path):
    from forge3d.backends import blender as blender_module

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        from forge3d.platform_support import RunResult
        return RunResult(ok=False, returncode=1, output="", limits_applied=False)

    monkeypatch.setattr(blender_module, "run", fake_run)
    monkeypatch.setattr(blender_module, "find_binary", lambda *a, **kw: "/usr/bin/blender")
    monkeypatch.setenv("FORGE3D_CODE_TIMEOUT", "45")
    config._settings = None
    try:
        result = blender_module.BlenderBackend().generate(
            ModelSpec(name="b"), tmp_path, source="import bpy")
        assert captured.get("limits") is True, "Blender ran without limits"
        assert captured.get("timeout") == 45, "Blender ignored FORGE3D_CODE_TIMEOUT"

        # The label must match reality, whatever this machine has. GitHub runners
        # ship bubblewrap, so asserting "unavailable" failed on CI — the invariant
        # is the correspondence, not one particular outcome.
        isolation = result.meta["isolation"]
        if isolation == "bwrap":
            pass                                    # a real sandbox: nothing more to claim
        elif isolation == "rlimit":
            from forge3d.platform_support import IS_WINDOWS

            assert not IS_WINDOWS, "rlimit claimed on a platform without rlimits"
        else:
            assert "unavailable" in isolation, (
                f"isolation {isolation!r} claims something it did not do")
    finally:
        monkeypatch.delenv("FORGE3D_CODE_TIMEOUT", raising=False)
        config._settings = None


# =========================================================================
# Finding 5 — CadQuery parity between in-process and subprocess
# =========================================================================
def test_cadquery_runner_exposes_spec_like_the_inprocess_path():
    from forge3d.backends.cadquery_backend import RUNNER

    assert '"spec"' in RUNNER or "'spec'" in RUNNER
    assert "ModelSpec.from_dict" in RUNNER


def test_cadquery_checks_every_output_not_just_step():
    import inspect

    from forge3d.backends import cadquery_backend

    source = inspect.getsource(cadquery_backend.CadQueryBackend._generate_subprocess)
    assert "step_path" in source and "stl_path" in source
    assert "missing" in source, "a missing STL must not be reported as success"


def test_cadquery_subprocess_mode_is_actually_selected(monkeypatch, tmp_path):
    from forge3d.backends.cadquery_backend import CadQueryBackend

    if not CadQueryBackend().available():
        pytest.skip("cadquery not installed")

    monkeypatch.setenv("FORGE3D_EXEC_MODE", "subprocess")
    config._settings = None
    try:
        result = CadQueryBackend().generate(
            ModelSpec(name="cq"), tmp_path,
            source="import cadquery as cq\nresult = cq.Workplane('XY').box(1, 1, 1)")
        assert result.meta.get("mode") == "subprocess", (
            "the test asked for subprocess mode; accepting inprocess hides the regression")
    finally:
        monkeypatch.delenv("FORGE3D_EXEC_MODE", raising=False)
        config._settings = None


# =========================================================================
# P2 — heartbeat across the whole pipeline, Meshy cancel, temp cleanup
# =========================================================================
def test_auto_heartbeat_stops_once_the_job_is_terminal(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "done"})
    job.status = "running"
    store.save(job)

    store.auto_heartbeat(job.id, interval=0.05)
    time.sleep(0.15)
    job.status = SUCCEEDED
    store.save(job)
    stamp = store.get(job.id).updated_at

    time.sleep(0.3)
    assert store.get(job.id).updated_at == stamp, "heartbeat kept writing after the job ended"


def test_meshy_cancels_an_image_job_on_the_image_endpoint(monkeypatch):
    from forge3d.providers.meshy import MeshyProvider

    provider = MeshyProvider()
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_x")
    monkeypatch.setattr("forge3d.providers.base.requests.request",
                        lambda method, url, **kw: _Stream(200, body=b'{"result":"img-1"}'))
    provider.submit(GenerationRequest(prompt="", images=["http://x/a.png"]))

    seen = []

    def capture(method, url, **kw):
        seen.append(url)
        return _Stream(200)

    monkeypatch.setattr("forge3d.providers.base.requests.request", capture)
    assert provider.cancel("img-1") is True
    assert "image-to-3d" in seen[0], "an image job must not be cancelled on the text endpoint"


def test_downloaded_reference_images_are_cleaned_up(monkeypatch, tmp_path):
    """Cleanup is request-local; a module-global list deleted other requests' files."""
    from forge3d.backends import image_backend

    created = []

    def fake_resolve(spec, source):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix="forge3d-image-test-"))
        created.append(d)
        from PIL import Image
        Image.new("L", (8, 8)).save(d / "input.png")
        return d / "input.png", d

    monkeypatch.setattr(image_backend.ImageBackend, "_resolve_image",
                        staticmethod(fake_resolve))
    backends.get_backend("image").generate(
        ModelSpec(name="cleanup", references=["x"]), tmp_path, mode="relief", grid=32)
    assert created and not created[0].exists(), "the downloaded reference image was left behind"


def test_a_local_image_path_needs_no_cleanup(tmp_path):
    from PIL import Image

    from forge3d.backends import image_backend

    src = tmp_path / "local.png"
    Image.new("L", (16, 16)).save(src)
    path, temp_dir = image_backend.ImageBackend._resolve_image(
        ModelSpec(name="l", references=[str(src)]), None)
    assert path == src and temp_dir is None
    assert src.exists(), "a caller-supplied file must never be deleted"
