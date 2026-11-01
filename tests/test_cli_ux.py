"""The command line and the shape catalogue, as a user actually meets them.

These came from running the tool the way a new user would: `forge3d demo` tells you
to try `create --shape ...`, `list_shapes` hands you catalogue entries, and a
mistyped path should not produce thirty lines of traceback.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from forge3d import server
from forge3d.cli import build_parser


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE3D_WORKDIR", str(tmp_path / "work"))
    from forge3d import config

    config._settings = None
    yield tmp_path / "work"
    config._settings = None


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "forge3d", *args],
        capture_output=True, text=True, timeout=300, cwd=cwd,
        env={**__import__("os").environ},
    )


# --- the catalogue round-trip -------------------------------------------
def test_a_catalogue_entry_can_be_passed_back_to_create_shape(workdir):
    """`list_shapes()` returns entries; passing one to `create_shape` used to raise
    `AttributeError: 'dict' object has no attribute 'strip'`."""
    entry = server.list_shapes()["shapes"][0]
    out = server.create_shape(shape=entry, resolution=20, name="from-entry",
                              render_previews=False, make_viewer=False)
    assert out["ok"] is True, out.get("error")
    assert out["card"]["triangles"] > 0


def test_a_catalogue_entry_uses_its_default_params(workdir):
    entry = {"name": "torus_knot", "params": {"p": 2, "q": 3}}
    out = server.create_shape(shape=entry, resolution=20, name="params",
                              render_previews=False, make_viewer=False)
    assert out["ok"] is True, out.get("error")


def test_an_explicit_param_overrides_the_catalogue_default(workdir):
    entry = {"name": "torus_knot", "params": {"p": 2, "q": 3}}
    out = server.create_shape(shape=entry, params={"q": 5}, resolution=20,
                              name="override", render_previews=False, make_viewer=False)
    assert out["ok"] is True, out.get("error")


def test_every_catalogue_family_builds(workdir):
    """The catalogue is a promise; every entry must honour it."""
    failures = []
    for entry in server.list_shapes()["shapes"]:
        out = server.create_shape(shape=entry, resolution=18,
                                  name=f"c-{entry['name']}",
                                  render_previews=False, make_viewer=False)
        if not out.get("ok"):
            failures.append((entry["name"], out.get("error")))
    assert not failures, failures


@pytest.mark.parametrize("bad", [42, None, ["torus_knot"]])
def test_a_non_string_shape_reports_cleanly(bad, workdir):
    out = server.create_shape(shape=bad)
    assert out["ok"] is False
    assert "shape must be a string" in out["error"] or "shape name" in out["error"]


# --- the CLI -------------------------------------------------------------
def test_create_accepts_a_shape(workdir):
    args = build_parser().parse_args(
        ["create", "--shape", "torus_knot", "--name", "cli-knot", "--resolution", "32"])
    assert args.shape == "torus_knot"
    assert args.func(args) == 0
    produced = list(workdir.rglob("cli-knot.glb"))
    assert produced, "create --shape produced no glb"


def test_create_accepts_shape_params(workdir):
    args = build_parser().parse_args(
        ["create", "--shape", "torus_knot", "--param", "q=4", "--name", "param-knot",
         "--resolution", "32"])
    assert args.func(args) == 0


def test_a_malformed_param_is_a_usage_error(workdir, capsys):
    args = build_parser().parse_args(["create", "--shape", "torus_knot", "--param", "oops"])
    assert args.func(args) == 2
    assert "key=value" in capsys.readouterr().err


def test_a_missing_file_does_not_print_a_traceback(workdir):
    """One line naming the problem, not a stack trace the user has to read past."""
    result = run_cli("validate", str(workdir / "nope.glb"))
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "no such file" in result.stderr


def test_an_unsupported_format_does_not_print_a_traceback(workdir):
    odd = workdir / "thing.xyz"
    odd.parent.mkdir(parents=True, exist_ok=True)
    odd.write_text("not a mesh", encoding="utf-8")
    result = run_cli("validate", str(odd))
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_providers_check_reports_rather_than_failing():
    """A status check that exits non-zero looks like a crash to the user."""
    result = run_cli("providers", "--check")
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout or "checks" in result.stdout


def test_the_demo_hint_names_a_command_that_works(workdir, capsys):
    """`forge3d demo` used to advertise `create --prompt ... --backend procedural`,
    which cannot work: that backend needs source code."""
    args = build_parser().parse_args(["demo", "--resolution", "24"])
    assert args.func(args) == 0
    printed = capsys.readouterr().out
    assert "--shape" in printed
    assert "--backend procedural" not in printed
