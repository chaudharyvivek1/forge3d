import pytest
from conftest import skip_without_blender, skip_without_cadquery, skip_without_openscad

from forge3d import backends
from forge3d.spec import ModelSpec

CUBE_SOURCE = """
import trimesh

def build():
    return trimesh.creation.box(extents=[10, 10, 10])
"""

BAD_SOURCE = """
def build(:
    return None
"""

NO_BUILD_SOURCE = """
x = 1
"""


def test_all_five_backends_registered():
    assert set(backends.ALL) == {"procedural", "openscad", "blender", "cadquery", "hosted"}


def test_describe_reports_availability_honestly():
    for entry in backends.list_backends():
        assert isinstance(entry["available"], bool)
        assert entry["name"]
        assert entry["capabilities"], f"{entry['name']} has no capabilities declared"


def test_procedural_is_always_available():
    assert backends.get_backend("procedural").available() is True


def test_unknown_backend_raises():
    with pytest.raises(KeyError):
        backends.get_backend("magic")


def test_procedural_builds_a_cube(tmp_path):
    spec = ModelSpec(name="cube", purpose="game")
    result = backends.get_backend("procedural").generate(spec, tmp_path, source=CUBE_SOURCE)
    assert result.ok, result.error
    assert result.path.endswith(".glb")
    from pathlib import Path

    assert Path(result.path).exists()
    assert Path(result.extra_outputs["stl"]).exists()


def test_procedural_syntax_error_is_reported_not_raised(tmp_path):
    spec = ModelSpec(name="broken")
    result = backends.get_backend("procedural").generate(spec, tmp_path, source=BAD_SOURCE)
    assert result.ok is False
    assert result.error
    assert result.log  # the agent needs the traceback to self-correct


def test_procedural_without_build_function(tmp_path):
    result = backends.get_backend("procedural").generate(ModelSpec(), tmp_path, source=NO_BUILD_SOURCE)
    assert result.ok is False
    assert "build()" in (result.error or "")


def test_procedural_without_source(tmp_path):
    result = backends.get_backend("procedural").generate(ModelSpec(), tmp_path)
    assert result.ok is False
    assert "source" in (result.error or "")


def test_procedural_respects_code_exec_flag(tmp_path, monkeypatch):
    from forge3d import config

    monkeypatch.setenv("FORGE3D_ALLOW_CODE_EXEC", "false")
    config._settings = None
    result = backends.get_backend("procedural").generate(ModelSpec(), tmp_path, source=CUBE_SOURCE)
    assert result.ok is False
    assert "disabled" in (result.error or "")
    config._settings = None


def test_procedural_accepts_a_list_of_parts(tmp_path):
    source = """
import trimesh
def build():
    a = trimesh.creation.box(extents=[1, 1, 1])
    b = trimesh.creation.box(extents=[1, 1, 1])
    b.apply_translation([2, 0, 0])
    return [a, b]
"""
    result = backends.get_backend("procedural").generate(ModelSpec(name="pair"), tmp_path, source=source)
    assert result.ok
    from forge3d import meshio

    st = meshio.stats(meshio.load(result.path))
    assert st["geometry_count"] == 2


# --- auto routing ---------------------------------------------------------
def test_auto_prefers_procedural_offline():
    picked = backends.resolve_auto(ModelSpec(purpose="game"), source=CUBE_SOURCE)
    assert picked == "procedural"


def test_auto_detects_blender_source():
    assert backends.resolve_auto(ModelSpec(), source="import bpy\nbpy.ops.mesh...") == "blender"


def test_auto_detects_cadquery_source():
    assert backends.resolve_auto(ModelSpec(), source="import cadquery as cq\nresult = cq.Workplane()") == "cadquery"


def test_auto_detects_openscad_source():
    src = "module foo() { cube(1); }\ndifference() { cube(2); cylinder(3); }"
    assert backends.resolve_auto(ModelSpec(), source=src) == "openscad"


# --- engines that need external binaries ----------------------------------
@skip_without_openscad
def test_openscad_bracket_builds(tmp_path):
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "examples" / "openscad_bracket.scad"
    result = backends.get_backend("openscad").generate(
        ModelSpec(name="bracket", purpose="print"), tmp_path, source=source.read_text()
    )
    assert result.ok, result.log


@skip_without_blender
def test_blender_gear_builds(tmp_path):
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "examples" / "blender_gear.py"
    result = backends.get_backend("blender").generate(
        ModelSpec(name="gear", purpose="game"), tmp_path, source=source.read_text(), timeout=300
    )
    assert result.ok, result.log


@skip_without_cadquery
def test_cadquery_plate_builds_step(tmp_path):
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "examples" / "cadquery_plate.py"
    result = backends.get_backend("cadquery").generate(
        ModelSpec(name="plate", purpose="cad"), tmp_path, source=source.read_text()
    )
    assert result.ok, result.error
    assert Path(result.extra_outputs["step"]).exists()


def test_hosted_unavailable_without_url():
    assert backends.get_backend("hosted").available() is False
