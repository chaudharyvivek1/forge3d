"""The local/cloud guarantee, enforced rather than documented.

An external review found that `spec.mode` existed, the router honoured it, and no
public interface could set it — and that naming a backend explicitly walked
straight past the check. These tests lock the whole matrix down.
"""

import pytest

from forge3d import backends, server
from forge3d.backends.registry import mode_violation
from forge3d.spec import ModelSpec

CUBE = "import trimesh\ndef build(): return trimesh.creation.box()"


# --- the matrix ----------------------------------------------------------
@pytest.mark.parametrize("mode,backend,blocked", [
    ("local", "hosted", True),      # the one that costs money
    ("local", "procedural", False),
    ("local", "image", False),
    ("cloud", "procedural", True),  # cloud means hosted only
    ("cloud", "hosted", False),
    ("auto", "hosted", False),
    ("auto", "procedural", False),
])
def test_mode_violation_matrix(mode, backend, blocked):
    reason = mode_violation(mode, backend)
    assert (reason is not None) is blocked, f"{mode}/{backend}: {reason}"


def test_violation_message_names_the_way_out():
    reason = mode_violation("local", "hosted")
    assert "mode='local'" in reason and "mode='auto'" in reason


# --- run() enforces it even with an explicit backend --------------------
def test_explicit_hosted_backend_is_refused_in_local_mode(tmp_path):
    _name, result = backends.run("hosted", ModelSpec(mode="local"), tmp_path)
    assert result.ok is False
    assert "mode='local'" in result.error


def test_explicit_local_backend_is_refused_in_cloud_mode(tmp_path):
    _name, result = backends.run("procedural", ModelSpec(mode="cloud"), tmp_path)
    assert result.ok is False
    assert "mode='cloud'" in result.error


def test_allowed_combination_still_builds(tmp_path):
    _name, result = backends.run("procedural", ModelSpec(name="ok", mode="local"),
                                 tmp_path, source=CUBE)
    assert result.ok, result.error


# --- MCP surface ---------------------------------------------------------
def test_create_model_accepts_mode_and_refuses_hosted(tmp_path):
    out = server.create_model(name="guarded", backend="hosted", mode="local",
                              prompt="a robot", render_previews=False, make_viewer=False)
    assert out["ok"] is False
    assert "mode='local'" in out["error"]


def test_create_model_records_mode_in_the_job(tmp_path):
    out = server.create_model(name="modejob", backend="procedural", mode="local",
                              source=CUBE, render_previews=False, make_viewer=False)
    assert out["ok"], out
    job = server.get_job(out["job_id"])["job"]
    assert job["spec"]["mode"] == "local"


def test_plan_model_accepts_mode():
    plan = server.plan_model(prompt="a logo", purpose="web", mode="local")
    assert plan["spec"]["mode"] == "local"
    assert plan["recommended_backend"] != "hosted"


def test_plan_model_defaults_to_auto():
    plan = server.plan_model(prompt="a bracket", purpose="print")
    assert plan["spec"]["mode"] == "auto"


def test_unknown_mode_is_rejected_by_the_spec():
    assert ModelSpec(mode="quantum").mode == "auto"


# --- CLI -----------------------------------------------------------------
def test_cli_exposes_the_mode_flag():
    from forge3d.cli import build_parser

    args = build_parser().parse_args(["create", "--mode", "local", "--name", "x"])
    assert args.mode == "local"


def test_cli_mode_defaults_to_auto():
    from forge3d.cli import build_parser

    assert build_parser().parse_args(["plan"]).mode == "auto"


def test_cli_rejects_a_nonsense_mode():
    from forge3d.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["create", "--mode", "cheap"])
