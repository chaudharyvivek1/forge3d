"""The concurrency slot must cover the whole generation pipeline.

v0.6.4 released the slot as soon as the backend returned, so preview rendering,
validation and viewer generation ran outside the limit. With `MAX_CONCURRENT=2`
you could have four pipelines in flight. These tests pin the slot to the entire
pipeline and prove every exit path releases it exactly once.

Synchronisation is done with events, not sleeps, so the assertions are about
ordering rather than about timing.
"""

import threading

import pytest

from forge3d import config, server
from forge3d.server import _slots

CUBE = "import trimesh\ndef build(): return trimesh.creation.box(extents=[10,10,10])"


@pytest.fixture(autouse=True)
def _fresh_settings():
    config._settings = None
    yield
    config._settings = None


def _generate(**over):
    kwargs = {"name": "slot", "backend": "procedural", "source": CUBE,
              "render_previews": False, "make_viewer": False}
    kwargs.update(over)
    return server.create_model(**kwargs)


def _slot_is_free(limit: int) -> bool:
    sem = _slots(limit)
    acquired = sem.acquire(timeout=1)
    if acquired:
        sem.release()
    return acquired


# --- the slot now spans post-processing ---------------------------------
def test_a_second_generation_is_refused_while_the_first_renders_previews(monkeypatch):
    """The regression: the slot used to be free once the backend returned."""
    rendering = threading.Event()
    release = threading.Event()

    def slow_render(*args, **kwargs):
        rendering.set()
        release.wait(timeout=30)
        return {"previews": {}}

    monkeypatch.setattr("forge3d.render.render_views", slow_render)
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    monkeypatch.setenv("FORGE3D_QUEUE_WAIT", "1")
    config._settings = None

    first = {}
    worker = threading.Thread(target=lambda: first.update(
        _generate(name="first", render_previews=True)))
    worker.start()
    try:
        assert rendering.wait(timeout=30), "the first pipeline never reached rendering"

        second = _generate(name="second")
        assert second["ok"] is False
        assert "server busy" in second["error"]
    finally:
        release.set()
        worker.join(timeout=30)

    assert first.get("ok") is True, first


def test_the_slot_is_held_across_validation(monkeypatch):
    validating = threading.Event()
    release = threading.Event()

    def slow_validate(*args, **kwargs):
        validating.set()
        release.wait(timeout=30)
        from forge3d.validate import validate as real

        return real(*args, **kwargs)

    monkeypatch.setattr("forge3d.server.validate.validate", slow_validate)
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    monkeypatch.setenv("FORGE3D_QUEUE_WAIT", "1")
    config._settings = None

    worker = threading.Thread(target=lambda: _generate(name="validating"))
    worker.start()
    try:
        assert validating.wait(timeout=30), "never reached validation"
        second = _generate(name="blocked-by-validation")
        assert "server busy" in second["error"]
    finally:
        release.set()
        worker.join(timeout=30)


# --- every exit path releases exactly once ------------------------------
def test_a_successful_generation_releases_its_slot():
    out = _generate(name="ok-release")
    assert out["ok"] is True
    assert _slot_is_free(1), "the slot leaked on success"


def test_a_backend_exception_releases_its_slot(monkeypatch):
    class Exploding:
        name = "exploding"
        executes_user_code = False

        def generate(self, spec, out_dir, source=None, **kw):
            raise RuntimeError("backend exploded")

    from forge3d import backends

    monkeypatch.setitem(backends.registry.ALL, "exploding", Exploding())
    out = _generate(name="boom", backend="exploding", source="x")
    assert out["ok"] is False
    assert _slot_is_free(1), "the slot leaked on a backend exception"


def test_an_unreadable_output_releases_its_slot(monkeypatch):
    def broken_load(path):
        raise OSError("truncated file")

    monkeypatch.setattr("forge3d.server.meshio.load", broken_load)
    out = _generate(name="unreadable")
    assert out["ok"] is False
    assert "unreadable" in out["error"]
    assert _slot_is_free(1), "the slot leaked when the output could not be re-opened"


def test_a_validation_exception_releases_its_slot(monkeypatch):
    def exploding_validate(*args, **kwargs):
        raise RuntimeError("validator bug")

    monkeypatch.setattr("forge3d.server.validate.validate", exploding_validate)
    out = _generate(name="bad-validate")
    assert out["ok"] is False
    assert "could not validate" in out["error"]
    assert _slot_is_free(1), "the slot leaked when validation raised"


def test_a_preview_failure_releases_its_slot(monkeypatch):
    def exploding_render(*args, **kwargs):
        raise RuntimeError("no matplotlib")

    monkeypatch.setattr("forge3d.render.render_views", exploding_render)
    out = _generate(name="bad-preview", render_previews=True)
    # Previews are a nicety: the mesh is the deliverable, so this still succeeds.
    assert out["ok"] is True
    assert _slot_is_free(1), "the slot leaked when preview rendering failed"


def test_a_viewer_failure_releases_its_slot(monkeypatch):
    def exploding_viewer(*args, **kwargs):
        raise RuntimeError("three.js template missing")

    monkeypatch.setattr("forge3d.server.viewer.build_viewer", exploding_viewer)
    out = _generate(name="bad-viewer", make_viewer=True)
    assert out["ok"] is True
    assert out["viewer"] is None
    assert _slot_is_free(1), "the slot leaked when viewer generation failed"


def test_repeated_failures_do_not_exhaust_the_pool(monkeypatch):
    """A leak of one permit per failure would wedge the server after N calls."""
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    config._settings = None

    for i in range(6):
        out = _generate(name=f"fail{i}", backend="definitely-not-a-backend")
        assert out["ok"] is False
        assert _slot_is_free(1), f"permit leaked on iteration {i}"


def test_the_release_happens_exactly_once(monkeypatch):
    """A double release would raise ValueError on a BoundedSemaphore."""
    releases = {"n": 0}
    real_release = None

    from forge3d.server import _slots as slots_factory

    sem = slots_factory(1)
    real_release = sem.release

    def counting_release():
        releases["n"] += 1
        real_release()

    monkeypatch.setattr(sem, "release", counting_release)
    monkeypatch.setattr("forge3d.server._slots", lambda limit: sem)
    config._settings = None

    out = _generate(name="counted")
    assert out["ok"] is True
    assert releases["n"] == 1, f"released {releases['n']} times for one generation"
