"""Concurrency: shared state, slot accounting, and parallel jobs.

v0.6.3 shipped a real race — a module-global list of temp directories, so one
generation's cleanup deleted another's reference image. These tests exist so that
class of bug fails here rather than in production.
"""

import json
import threading
import time

from forge3d import backends, config, server
from forge3d.jobs import RUNNING, SUCCEEDED, JobStore

CUBE = "import trimesh\ndef build(): return trimesh.creation.box()"


# --- request-local temp dirs --------------------------------------------
def test_parallel_image_generations_do_not_delete_each_others_files(monkeypatch, tmp_path):
    """The v0.6.3 bug: `_cleanup_temp()` popped every dir, not just its own."""
    import tempfile
    from pathlib import Path

    from PIL import Image

    from forge3d.backends import image_backend

    made = []

    def fake_resolve(spec, source):
        d = Path(tempfile.mkdtemp(prefix=f"forge3d-par-{spec.name}-"))
        made.append(d)
        Image.new("L", (32, 32)).save(d / "input.png")
        return d / "input.png", d

    monkeypatch.setattr(image_backend.ImageBackend, "_resolve_image",
                        staticmethod(fake_resolve))

    results = {}

    def run(name):
        results[name] = backends.get_backend("image").generate(
            spec=__import__("forge3d").spec.ModelSpec(name=name),
            out_dir=tmp_path / name, mode="relief", grid=32)

    threads = [threading.Thread(target=run, args=(f"img{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert all(r.ok for r in results.values()), {k: v.error for k, v in results.items()}
    assert all(not d.exists() for d in made), "a temp dir survived"
    assert len(made) == 4


# --- concurrency slots ---------------------------------------------------
class _SlowBackend:
    """Stands in for a slow generation, so slot accounting can be observed."""

    name = "slowtest"
    executes_user_code = False

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, spec, out_dir, source=None, **kw):
        from forge3d.backends.base import BackendResult

        self.started.set()
        self.release.wait(timeout=30)
        return BackendResult(ok=False, error="slow test backend")


def test_a_second_request_is_told_the_server_is_busy(monkeypatch):
    slow = _SlowBackend()
    monkeypatch.setitem(backends.registry.ALL, "slowtest", slow)
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    monkeypatch.setenv("FORGE3D_QUEUE_WAIT", "1")
    config._settings = None
    try:
        first = {}
        thread = threading.Thread(
            target=lambda: first.update(server.create_model(
                name="busy-a", backend="slowtest", source="x",
                render_previews=False, make_viewer=False)))
        thread.start()
        assert slow.started.wait(timeout=10)

        second = server.create_model(name="busy-b", backend="slowtest", source="x",
                                     render_previews=False, make_viewer=False)
        assert second["ok"] is False
        assert "server busy" in second["error"]

        slow.release.set()
        thread.join(timeout=30)
        assert first["ok"] is False  # the stub backend always fails
    finally:
        for var in ("FORGE3D_MAX_CONCURRENT", "FORGE3D_QUEUE_WAIT"):
            monkeypatch.delenv(var, raising=False)
        config._settings = None


def test_a_slot_is_released_even_when_the_backend_raises(monkeypatch):
    """A leaked slot would wedge the server after one crash."""

    class Exploding(_SlowBackend):
        name = "exploding"

        def generate(self, spec, out_dir, source=None, **kw):
            raise RuntimeError("backend exploded")

    monkeypatch.setitem(backends.registry.ALL, "exploding", Exploding())
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "1")
    config._settings = None
    try:
        out = server.create_model(name="boom", backend="exploding", source="x",
                                  render_previews=False, make_viewer=False)
        assert out["ok"] is False

        # If the slot leaked, this second call would block for queue_wait and
        # return "server busy" instead of running.
        from forge3d.server import _slots

        assert _slots(1).acquire(timeout=1), "the concurrency slot leaked"
        _slots(1).release()
    finally:
        monkeypatch.delenv("FORGE3D_MAX_CONCURRENT", raising=False)
        config._settings = None


# --- job store under contention -----------------------------------------
def test_concurrent_writers_never_corrupt_a_job_record(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("procedural", {"name": "contended"})
    job.status = RUNNING
    store.save(job)

    errors = []

    def hammer():
        try:
            for _ in range(25):
                store.touch(job.id)
                assert json.loads(store._path(job.id).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    record = json.loads(store._path(job.id).read_text(encoding="utf-8"))
    assert record["status"] == RUNNING
    assert record["id"] == job.id


def test_parallel_generations_of_the_same_name_get_separate_directories(monkeypatch):
    monkeypatch.setenv("FORGE3D_MAX_CONCURRENT", "4")
    config._settings = None
    try:
        paths = []
        lock = threading.Lock()

        def run():
            out = server.create_model(name="same-name", backend="procedural",
                                      source=CUBE, render_previews=False,
                                      make_viewer=False)
            with lock:
                paths.append(out.get("path"))

        threads = [threading.Thread(target=run) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        real = [p for p in paths if p]
        assert len(real) == 3
        assert len(set(real)) == 3, f"output directories collided: {real}"
    finally:
        monkeypatch.delenv("FORGE3D_MAX_CONCURRENT", raising=False)
        config._settings = None


# --- heartbeat under concurrency ----------------------------------------
def test_many_heartbeats_do_not_deadlock_the_store(tmp_path):
    store = JobStore(tmp_path / "jobs")
    jobs = []
    for i in range(5):
        job = store.create("procedural", {"name": f"hb{i}"})
        job.status = RUNNING
        store.save(job)
        jobs.append(job)
        store.auto_heartbeat(job.id, interval=0.05)

    time.sleep(0.3)
    for job in jobs:
        job.status = SUCCEEDED
        store.save(job)

    started = time.time()
    for job in jobs:
        assert store.get(job.id).status == SUCCEEDED
    assert time.time() - started < 5, "the store is contended by heartbeat threads"


# --- the one-command demo ------------------------------------------------
def test_the_demo_command_runs_with_no_arguments(capsys, monkeypatch, tmp_path):
    """`forge3d demo` is the advertised one-command try-it; it must actually work."""
    monkeypatch.setenv("FORGE3D_WORKDIR", str(tmp_path / "work"))
    config._settings = None
    try:
        from forge3d.cli import build_parser

        args = build_parser().parse_args(["demo", "--resolution", "48"])
        code = args.func(args)
        out = capsys.readouterr().out

        assert code == 0
        assert "watertight True" in out
        assert "validation PASS" in out
        assert "viewer" in out
    finally:
        monkeypatch.delenv("FORGE3D_WORKDIR", raising=False)
        config._settings = None
