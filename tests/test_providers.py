"""Provider adapters: request mapping, status normalisation, and failure handling.

No network. Each commercial adapter is driven with the response shapes its public
docs describe, so the *mapping* logic is tested even though the live endpoints are
not — which is the honest split, given none of them have been exercised against a
real account here.
"""

import json

import pytest

from forge3d import backends, server
from forge3d.providers import registry
from forge3d.providers.base import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    GenerationProvider,
    GenerationRequest,
    ProviderError,
    ProviderJob,
    normalise_status,
)
from forge3d.providers.meshy import MeshyProvider
from forge3d.providers.rodin import RodinProvider
from forge3d.providers.tripo import TripoProvider
from forge3d.spec import ModelSpec


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)
        self.content = self.text.encode()

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status on {self.status_code}")

    def iter_content(self, chunk_size=1024):
        yield b"glTF-binary-placeholder"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_http(monkeypatch):
    """Replace requests.request with a scripted list of responses."""
    calls = {"queue": [], "made": []}

    def request(method, url, **kwargs):
        calls["made"].append({"method": method, "url": url, "kwargs": kwargs})
        if not calls["queue"]:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        item = calls["queue"].pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(**item)

    monkeypatch.setattr("forge3d.providers.base.requests.request", request)
    monkeypatch.setattr("forge3d.providers.base.time.sleep", lambda *_: None)
    return calls


# --- status normalisation ------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("PENDING", QUEUED), ("queued", QUEUED), ("IN_PROGRESS", RUNNING),
    ("running", RUNNING), ("SUCCEEDED", SUCCEEDED), ("success", SUCCEEDED),
    ("Done", SUCCEEDED), ("FAILED", FAILED), ("cancelled", CANCELLED),
])
def test_status_aliases(raw, expected):
    assert normalise_status(raw) == expected


def test_unknown_status_is_running_not_failed():
    """An unrecognised word is not evidence of failure; aborting would be wrong."""
    assert normalise_status("wibble") == RUNNING
    assert normalise_status(None) == RUNNING


# --- meshy ---------------------------------------------------------------
def test_meshy_submit_maps_a_text_request(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_test")
    fake_http["queue"] = [{"payload": {"result": "task-1"}}]

    job = MeshyProvider().submit(GenerationRequest(prompt="a teapot", art_style="clay"))
    assert job.job_id == "task-1"
    body = fake_http["made"][0]["kwargs"]["json"]
    assert body["prompt"] == "a teapot" and body["art_style"] == "clay"
    assert body["mode"] == "preview"
    assert fake_http["made"][0]["url"].endswith("/openapi/v2/text-to-3d")


def test_meshy_submit_uses_the_image_endpoint(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_test")
    fake_http["queue"] = [{"payload": {"result": "task-2"}}]

    MeshyProvider().submit(GenerationRequest(prompt="", images=["https://x/a.png"]))
    call = fake_http["made"][0]
    assert call["url"].endswith("/openapi/v1/image-to-3d")
    assert call["kwargs"]["json"]["image_url"] == "https://x/a.png"


def test_meshy_status_maps_progress_and_asset(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_test")
    fake_http["queue"] = [{"payload": {
        "status": "SUCCEEDED", "progress": 100,
        "model_urls": {"glb": "https://cdn/x.glb"},
    }}]
    job = MeshyProvider().get_status("task-1")
    assert job.status == SUCCEEDED and job.progress == 1.0
    assert job.asset_url == "https://cdn/x.glb"


def test_meshy_cancel(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_MESHY_KEY", "msy_test")
    fake_http["queue"] = [{"status_code": 200}]
    assert MeshyProvider().cancel("task-1") is True
    assert fake_http["made"][0]["method"] == "DELETE"


def test_meshy_without_key_explains_which_variable(monkeypatch):
    monkeypatch.delenv("FORGE3D_MESHY_KEY", raising=False)
    with pytest.raises(ProviderError, match="FORGE3D_MESHY_KEY"):
        MeshyProvider().submit(GenerationRequest(prompt="x"))


# --- tripo ---------------------------------------------------------------
def test_tripo_uses_the_image_task_type(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_TRIPO_KEY", "tsk_test")
    fake_http["queue"] = [{"payload": {"code": 0, "data": {"task_id": "t-9"}}}]

    job = TripoProvider().submit(GenerationRequest(images=["https://x/a.png"]))
    assert job.job_id == "t-9"
    body = fake_http["made"][0]["kwargs"]["json"]
    assert body["type"] == "image_to_model"
    assert body["file"]["url"] == "https://x/a.png"


def test_tripo_application_error_beats_http_200(monkeypatch, fake_http):
    """HTTP 200 with code != 0 is a failure. This is the easiest thing to get wrong."""
    monkeypatch.setenv("FORGE3D_TRIPO_KEY", "tsk_test")
    fake_http["queue"] = [{"payload": {"code": 1004, "message": "bad token"}}]

    with pytest.raises(ProviderError, match="code 1004"):
        TripoProvider().submit(GenerationRequest(prompt="x"))


def test_tripo_status_prefers_the_pbr_model(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_TRIPO_KEY", "tsk_test")
    fake_http["queue"] = [{"payload": {"code": 0, "data": {
        "status": "success", "progress": 100,
        "output": {"model": "https://cdn/plain.glb", "pbr_model": "https://cdn/pbr.glb"},
    }}}]
    job = TripoProvider().get_status("t-9")
    assert job.status == SUCCEEDED
    assert job.asset_url == "https://cdn/pbr.glb"


# --- rodin ---------------------------------------------------------------
def test_rodin_reads_the_uuid(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_RODIN_KEY", "rk_test")
    fake_http["queue"] = [{"payload": {"uuid": "r-1"}}]
    assert RodinProvider().submit(GenerationRequest(prompt="a rock")).job_id == "r-1"


def test_rodin_accepts_a_jobs_wrapper(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_RODIN_KEY", "rk_test")
    fake_http["queue"] = [{"payload": {"jobs": {"uuids": ["r-2"]}}}]
    assert RodinProvider().submit(GenerationRequest(prompt="x")).job_id == "r-2"


def test_rodin_status_is_a_post_with_a_subscription_key(monkeypatch, fake_http):
    monkeypatch.setenv("FORGE3D_RODIN_KEY", "rk_test")
    fake_http["queue"] = [{"payload": {"jobs": [
        {"uuid": "r-1", "status": "Done", "progress": 100,
         "file": {"glb": "https://cdn/r.glb"}},
    ]}}]
    job = RodinProvider().get_status("r-1")
    assert job.status == SUCCEEDED and job.asset_url == "https://cdn/r.glb"
    call = fake_http["made"][0]
    assert call["method"] == "POST" and call["kwargs"]["json"]["subscription_key"] == "r-1"


def test_rodin_needs_a_prompt_or_an_image(monkeypatch):
    monkeypatch.setenv("FORGE3D_RODIN_KEY", "rk_test")
    with pytest.raises(ProviderError, match="prompt or at least one image"):
        RodinProvider().submit(GenerationRequest())


def test_generic_worker_sync_submit_returns_a_finished_job(monkeypatch, fake_http):
    """A worker may answer with an asset immediately instead of a job id.

    This branch was untested and referenced an unimported name — lint found it,
    this test keeps it found.
    """
    from forge3d.providers.generic import GenericWorkerProvider

    monkeypatch.setenv("FORGE3D_HOSTED_URL", "http://worker.local")
    fake_http["queue"] = [{"payload": {"asset_url": "http://worker.local/a.glb"}}]

    job = GenericWorkerProvider().submit(GenerationRequest(prompt="x"))
    assert job.status == SUCCEEDED
    assert job.done is True
    assert job.asset_url == "http://worker.local/a.glb"


def test_generic_worker_async_submit_returns_a_job_id(monkeypatch, fake_http):
    from forge3d.providers.generic import GenericWorkerProvider

    monkeypatch.setenv("FORGE3D_HOSTED_URL", "http://worker.local")
    fake_http["queue"] = [{"payload": {"job_id": "w-1"}}]
    job = GenericWorkerProvider().submit(GenerationRequest(prompt="x"))
    assert job.job_id == "w-1" and job.done is False


# --- registry ------------------------------------------------------------
def test_registry_lists_every_provider_with_verification_state():
    entries = {p["name"]: p for p in registry.list_providers()}
    assert set(entries) >= {"meshy", "tripo", "rodin", "generic"}
    assert entries["generic"]["verified"] is True
    for name in ("meshy", "tripo", "rodin"):
        assert entries[name]["verified"] is False, "commercial adapters must not claim verification"


def test_registry_resolves_an_explicit_name():
    assert registry.resolve("tripo").name == "tripo"


def test_registry_rejects_an_unknown_name():
    with pytest.raises(KeyError, match="choose from"):
        registry.resolve("midjourney")


def test_registry_explains_what_to_configure(monkeypatch):
    for var in ("FORGE3D_MESHY_KEY", "FORGE3D_TRIPO_KEY", "FORGE3D_RODIN_KEY",
                "FORGE3D_HOSTED_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderError) as exc:
        registry.resolve()
    message = str(exc.value)
    assert "FORGE3D_MESHY_KEY" in message and "FORGE3D_HOSTED_URL" in message


def test_registry_auto_prefers_a_configured_commercial_provider(monkeypatch):
    monkeypatch.setenv("FORGE3D_TRIPO_KEY", "tsk_test")
    monkeypatch.setenv("FORGE3D_HOSTED_URL", "http://localhost:1")
    assert registry.resolve().name == "tripo"


# --- hosted backend end-to-end with a fake provider ---------------------
class _FakeProvider(GenerationProvider):
    name = "fake"
    verified = True
    key_env = None

    def __init__(self):
        self.polls = 0

    def available(self):
        return True

    def submit(self, request):
        assert request.prompt
        return ProviderJob(provider=self.name, job_id="fake-1", status=QUEUED)

    def get_status(self, job_id):
        self.polls += 1
        if self.polls < 2:
            return ProviderJob(provider=self.name, job_id=job_id, status=RUNNING, progress=0.5)
        return ProviderJob(provider=self.name, job_id=job_id, status=SUCCEEDED,
                           progress=1.0, asset_url="https://cdn/fake.glb")

    def download(self, job, out_dir, name):
        from pathlib import Path

        out = Path(out_dir) / f"{name}.glb"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"glTF-fake")
        return out


def test_hosted_backend_runs_a_provider_to_completion(monkeypatch, tmp_path):
    fake = _FakeProvider()
    registry.register(fake)
    monkeypatch.setenv("FORGE3D_PROVIDER", "fake")
    try:
        from forge3d import config

        config._settings = None
        result = backends.get_backend("hosted").generate(
            ModelSpec(name="hosted-e2e", prompt="a teapot"), tmp_path)
        assert result.ok, result.error
        assert fake.polls >= 1
        assert result.meta["provider"] == "fake"
        assert result.meta["provider_verified"] is True
    finally:
        monkeypatch.delenv("FORGE3D_PROVIDER", raising=False)
        registry._REGISTRY.pop("fake", None)
        config._settings = None


def test_hosted_backend_reports_a_failed_job(monkeypatch, tmp_path):
    class Failing(_FakeProvider):
        name = "failing"

        def get_status(self, job_id):
            return ProviderJob(provider=self.name, job_id=job_id, status=FAILED,
                               error="provider says no")

    registry.register(Failing())
    monkeypatch.setenv("FORGE3D_PROVIDER", "failing")
    try:
        from forge3d import config

        config._settings = None
        result = backends.get_backend("hosted").generate(
            ModelSpec(name="fails", prompt="x"), tmp_path)
        assert result.ok is False
        assert "provider says no" in result.error
    finally:
        monkeypatch.delenv("FORGE3D_PROVIDER", raising=False)
        registry._REGISTRY.pop("failing", None)
        config._settings = None


# --- MCP surface ---------------------------------------------------------
def test_list_providers_tool():
    out = server.list_providers()
    assert out["ok"]
    names = [p["name"] for p in out["providers"]]
    assert "meshy" in names
    assert "meshy" in out["unverified"]
