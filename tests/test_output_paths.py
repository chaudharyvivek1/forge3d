"""Output paths are confined to the work directory.

Every tool that takes an output path used to write wherever it was told:
`make_viewer(out_html="/tmp/x.html")` wrote to `/tmp`. On a laptop that is
convenience; in HTTP mode, where the caller may not be trusted, it is an
arbitrary-write primitive — `/etc/cron.d/`, `~/.ssh/authorized_keys`, a systemd
unit.

These tests pin the guard, the overwrite protection and the input validation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge3d import server


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("FORGE3D_WORKDIR", str(root))
    monkeypatch.delenv("FORGE3D_ALLOW_OUTSIDE_WORKDIR", raising=False)
    from forge3d import config

    config._settings = None
    yield root
    config._settings = None


@pytest.fixture
def model(workdir):
    return server.create_shape(shape="spring", resolution=24, name="guard",
                               render_previews=False, make_viewer=False)["path"]


# --- 1. writes outside the work directory are refused -------------------
def test_make_viewer_refuses_an_outside_path(model, tmp_path):
    outside = tmp_path / "OUTSIDE_viewer.html"
    out = server.make_viewer(model, out_html=str(outside))
    assert out["ok"] is False
    assert "refusing to write outside" in out["error"]
    assert not outside.exists(), "the file was written anyway"


def test_make_scene_viewer_refuses_an_outside_path(model, tmp_path):
    outside = tmp_path / "OUTSIDE_scene.html"
    out = server.make_scene_viewer([{"path": model}], out_html=str(outside))
    assert out["ok"] is False
    assert not outside.exists()


def test_create_handoff_refuses_an_outside_path(tmp_path):
    outside = tmp_path / "OUTSIDE_handoff.json"
    out = server.create_handoff({"name": "x"}, out_path=str(outside))
    assert out["ok"] is False
    assert not outside.exists()
    assert not outside.with_suffix(".md").exists(), "the markdown escaped too"


def test_repair_model_refuses_an_outside_path(model, tmp_path):
    outside = tmp_path / "OUTSIDE_repaired.glb"
    out = server.repair_model(model, out_path=str(outside))
    assert out["ok"] is False
    assert not outside.exists()


def test_convert_model_refuses_an_outside_path(model, tmp_path):
    outside = tmp_path / "OUTSIDE_converted.stl"
    out = server.convert_model(model, "stl", out_path=str(outside))
    assert out["ok"] is False
    assert not outside.exists()


def test_a_symlink_out_of_the_workdir_is_refused(model, workdir, tmp_path):
    """`resolve()` follows the link, so planting one inside does not help."""
    link = workdir / "escape"
    os.symlink(str(tmp_path), str(link))
    out = server.make_viewer(model, out_html=str(link / "via_link.html"))
    assert out["ok"] is False
    assert not (tmp_path / "via_link.html").exists()


# --- the guard must not break legitimate use ----------------------------
def test_a_path_inside_the_workdir_still_works(model, workdir):
    target = workdir / "custom" / "page.html"
    out = server.make_viewer(model, out_html=str(target))
    assert out["ok"] is True, out
    assert target.exists()


def test_the_default_location_is_inside_the_workdir(model, workdir):
    out = server.make_viewer(model)
    assert out["ok"] is True
    assert Path(out["html"]).resolve().is_relative_to(workdir.resolve())


def test_the_escape_hatch_is_opt_in(model, tmp_path, monkeypatch):
    outside = tmp_path / "explicit.html"
    monkeypatch.setenv("FORGE3D_ALLOW_OUTSIDE_WORKDIR", "1")
    out = server.make_viewer(model, out_html=str(outside))
    assert out["ok"] is True, out
    assert outside.exists()
    monkeypatch.delenv("FORGE3D_ALLOW_OUTSIDE_WORKDIR", raising=False)


# --- 2. converting must never overwrite its input -----------------------
def test_converting_to_the_input_path_does_not_touch_the_input(model):
    before = Path(model).read_bytes()
    out = server.convert_model(model, "glb", out_path=model)
    assert out["ok"] is True, out
    assert Path(out["path"]) != Path(model), "wrote over the source file"
    assert Path(out["path"]).stem.endswith("_converted")
    assert Path(model).read_bytes() == before, "the original bytes changed"


def test_repairing_to_the_input_path_does_not_touch_the_input(model):
    before = Path(model).read_bytes()
    out = server.repair_model(model, out_path=model)
    assert out["ok"] is True, out
    assert Path(out["path"]) != Path(model)
    assert Path(model).read_bytes() == before


def test_converting_to_a_different_path_is_unchanged(model, workdir):
    target = workdir / "elsewhere" / "model.stl"
    out = server.convert_model(model, "stl", out_path=str(target))
    assert out["ok"] is True, out
    assert Path(out["path"]) == target.resolve()
    assert target.exists()


# --- 3. bad input is an error, not a traceback --------------------------
@pytest.mark.parametrize("bad", [None, 42, [], {}])
def test_a_non_string_path_is_reported_not_raised(bad):
    out = server.make_viewer(bad)
    assert out["ok"] is False
    assert "must be a string" in out["error"]


def test_a_missing_file_still_reports_cleanly(workdir):
    out = server.make_viewer(str(workdir / "nope.glb"))
    assert out["ok"] is False
    assert "no such file" in out["error"]
