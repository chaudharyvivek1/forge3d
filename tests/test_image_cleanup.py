"""Request-owned image temp directories are always cleaned up.

v0.6.4 started the cleanup region *after* `_load_image()`, so a corrupt or
unsupported image left the downloaded file on disk forever. The region now begins
the moment resolution succeeds.

Cleanup is also request-local: only the directory this call created is removed, so
a caller-supplied input file is never touched and two concurrent requests cannot
delete each other's files.
"""

import threading

import pytest
from PIL import Image

from forge3d import backends
from forge3d.backends import image_backend
from forge3d.spec import ModelSpec


@pytest.fixture
def fake_download(monkeypatch):
    """Stand in for a remote fetch: creates a temp dir the request owns."""
    import tempfile
    from pathlib import Path

    made = []

    def _install(payload: bytes | None = None, write_real: bool = True):
        def resolve(spec, source):
            d = Path(tempfile.mkdtemp(prefix="forge3d-cleanup-test-"))
            made.append(d)
            target = d / "input.png"
            if write_real:
                if payload is None:
                    Image.new("L", (16, 16)).save(target)
                else:
                    target.write_bytes(payload)
            return target, d

        monkeypatch.setattr(image_backend.ImageBackend, "_resolve_image",
                            staticmethod(resolve))
        return made

    return _install


def _run(tmp_path, name="cleanup", **kw):
    return backends.get_backend("image").generate(
        ModelSpec(name=name), tmp_path / name, mode="relief", grid=32, **kw)


# --- the regression: loading failure must still clean up ----------------
def test_a_temp_dir_is_removed_when_loading_fails(fake_download, tmp_path):
    """Corrupt bytes: `_load_image` raises, and the download must not survive."""
    made = fake_download(b"this is not a png")

    out = _run(tmp_path, name="corrupt")

    assert out.ok is False
    assert "could not read image" in out.error, "the error message changed"
    assert made and not made[0].exists(), "the downloaded file leaked on a decode failure"


def test_a_temp_dir_is_removed_for_an_unsupported_format(fake_download, tmp_path):
    """A file PIL cannot identify behaves the same way."""
    made = fake_download(b"\x00\x01\x02\x03 unsupported container")

    out = _run(tmp_path, name="unsupported")

    assert out.ok is False
    assert not made[0].exists()


# --- success path --------------------------------------------------------
def test_a_temp_dir_is_removed_after_successful_processing(fake_download, tmp_path):
    made = fake_download()

    out = _run(tmp_path, name="ok")

    assert out.ok is True, out.error
    assert not made[0].exists(), "the temp dir survived a successful run"
    assert (tmp_path / "ok" / "ok.glb").exists()


# --- ownership -----------------------------------------------------------
def test_a_caller_supplied_file_is_never_deleted(tmp_path):
    """`_resolve_image` returns temp_dir=None for a local path, so nothing is removed."""
    src = tmp_path / "mine.png"
    Image.new("L", (16, 16)).save(src)

    out = backends.get_backend("image").generate(
        ModelSpec(name="mine"), tmp_path / "out", source=str(src), mode="relief", grid=32)

    assert out.ok is True, out.error
    assert src.exists(), "a caller-owned input file was deleted"


def test_two_requests_do_not_remove_each_others_temp_dirs(fake_download, tmp_path):
    made = fake_download()

    results = {}

    def run(name):
        results[name] = _run(tmp_path, name=name)

    threads = [threading.Thread(target=run, args=(f"par{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert all(r.ok for r in results.values()), {k: v.error for k, v in results.items()}
    assert len(made) == 3
    assert all(not d.exists() for d in made)


# --- a later step raising still cleans up -------------------------------
def test_cleanup_happens_when_a_later_step_raises(fake_download, monkeypatch, tmp_path):
    made = fake_download()

    def exploding_build(*args, **kwargs):
        raise RuntimeError("geometry stage failed")

    monkeypatch.setattr(image_backend.ImageBackend, "_build", exploding_build)

    with pytest.raises(RuntimeError, match="geometry stage failed"):
        _run(tmp_path, name="explodes")

    assert not made[0].exists(), "the temp dir leaked when a later stage raised"


# --- no temp dir at all --------------------------------------------------
def test_nothing_is_created_when_no_image_is_given(tmp_path):
    out = backends.get_backend("image").generate(ModelSpec(name="none"), tmp_path)
    assert out.ok is False
    assert "needs an image" in out.error


# --- Windows file-lock behaviour ----------------------------------------
def test_loading_releases_the_file_handle(tmp_path):
    """`rmtree` cannot delete a directory whose file is still open on Windows."""
    src = tmp_path / "handle.png"
    Image.new("L", (8, 8), 7).save(src)

    img = image_backend.ImageBackend._load_image(src)
    src.unlink()                       # fails on Windows if the handle leaked

    assert img.size == (8, 8)
    assert img.getpixel((0, 0)) == (7, 7, 7, 255), "the returned image is not a real copy"
