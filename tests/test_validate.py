import trimesh

from forge3d import validate
from forge3d.spec import ModelSpec


def test_closed_cube_passes_print(cube):
    report = validate.validate(cube, profile="print")
    assert report.ok, report.to_dict()
    ids = {c.id for c in report.checks}
    assert "watertight" in ids and "winding_consistent" in ids


def test_open_surface_fails_print(open_surface):
    report = validate.validate(open_surface, profile="print")
    assert not report.ok
    failed = {c.id for c in report.checks if not c.ok}
    assert "watertight" in failed


def test_open_surface_is_a_warning_not_an_error_for_game(open_surface):
    report = validate.validate(open_surface, profile="game")
    assert report.ok  # watertightness is not a game requirement


def test_every_failure_has_a_hint(open_surface):
    report = validate.validate(open_surface, profile="print")
    for check in report.checks:
        if not check.ok:
            assert check.hint, f"{check.id} failed without a hint"


def test_face_budget_from_spec(cube):
    spec = ModelSpec(purpose="game", poly_budget=1)  # absurd on purpose
    report = validate.validate(cube, profile="game", spec=spec)
    failed = {c.id for c in report.checks if not c.ok}
    assert "face_budget" in failed


def test_face_budget_respects_profile_default(cube):
    report = validate.validate(cube, profile="web")
    passed = {c.id for c in report.checks if c.ok}
    assert "face_budget" in passed


def test_dimensions_drift_detected(cube):
    # cube is 10mm; ask for 100mm -> should warn
    spec = ModelSpec(purpose="print", dimensions={"x": 100, "y": 100, "z": 100})
    report = validate.validate(cube, profile="print", spec=spec)
    failed = {c.id for c in report.checks if not c.ok}
    assert "dimensions_match_spec" in failed


def test_scale_to_spec_hits_target(cube):
    spec = ModelSpec(purpose="print", dimensions={"x": 100, "y": 100, "z": 100})
    scaled = validate.scale_to_spec(cube, spec)
    assert abs(max(scaled.extents) - 100) < 0.01


def test_scale_to_spec_is_a_noop_without_dimensions(cube):
    spec = ModelSpec()
    assert validate.scale_to_spec(cube, spec) is cube


def test_unknown_profile_falls_back_to_any(cube):
    report = validate.validate(cube, profile="quantum")
    assert report.profile in validate.PROFILES


def test_empty_mesh_is_an_error():
    empty = trimesh.Trimesh()
    report = validate.validate(empty, profile="print")
    assert not report.ok


def test_summary_never_says_fail_without_errors(cube):
    report = validate.validate(cube, profile="print")
    if report.ok:
        assert "FAIL" not in report.summary()
