"""Parametric surfaces: every shape builds, and every *closed* one is a real solid.

The interesting failure mode for a shape library is not "it crashed" — it is "it
produced a mesh that looks right and is not watertight", which only shows up when
someone tries to print it. So the core test here iterates the whole catalogue and
asserts the manifold property each entry claims.
"""

import numpy as np
import pytest

from forge3d import meshio, server, validate
from forge3d.geometry.surfaces import SHAPES, build_shape, list_shapes
from forge3d.spec import ModelSpec

RES = 40  # small but enough to expose topology problems


# --- catalogue -----------------------------------------------------------
def test_catalogue_is_populated_and_documented():
    entries = list_shapes()
    assert len(entries) >= 15
    for entry in entries:
        assert entry["equation"], f"{entry['name']} has no equation"
        assert entry["params"] is not None
        assert entry["notes"], f"{entry['name']} has no notes"


def test_unknown_shape_names_the_alternatives():
    with pytest.raises(KeyError, match="choose from"):
        build_shape("definitely-not-a-shape")


# --- every shape ---------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_every_shape_builds(name):
    mesh = build_shape(name, resolution=RES)
    assert len(mesh.faces) > 8, f"{name} produced almost nothing"
    assert len(mesh.vertices) > 4
    assert np.isfinite(mesh.vertices).all(), f"{name} produced NaN/inf vertices"


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_closed_shapes_are_watertight(name):
    """The property that decides whether a shape can be printed."""
    spec = SHAPES[name]
    mesh = build_shape(name, resolution=RES)
    if spec.closed:
        assert mesh.is_watertight, f"{name} claims closed but leaks"
        assert mesh.is_winding_consistent, f"{name} has mixed normals"
        assert mesh.volume > 0, f"{name} has zero or negative volume"


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_shapes_are_deterministic(name):
    a = build_shape(name, resolution=RES)
    b = build_shape(name, resolution=RES)
    assert np.allclose(a.vertices, b.vertices)


def test_resolution_changes_detail():
    low = build_shape("supershape", resolution=24)
    high = build_shape("supershape", resolution=96)
    assert len(high.faces) > len(low.faces) * 3


def test_resolution_is_clamped():
    assert len(build_shape("supershape", resolution=1).faces) > 0
    assert len(build_shape("supershape", resolution=5000).faces) < 2_000_000


# --- parameter behaviour -------------------------------------------------
def test_supershape_m_changes_the_silhouette():
    star = build_shape("supershape", {"m": 5, "n1": 0.3}, resolution=48)
    round_ = build_shape("supershape", {"m": 2, "n1": 1.0}, resolution=48)
    assert not np.allclose(star.vertices, round_.vertices)


def test_superellipsoid_exponents_control_boxiness():
    boxy = build_shape("superellipsoid", {"e1": 0.2, "e2": 0.2}, resolution=48)
    round_ = build_shape("superellipsoid", {"e1": 1.0, "e2": 1.0}, resolution=48)
    # A boxy superellipsoid fills more of its bounding box than an ellipsoid does.
    boxy_fill = boxy.volume / np.prod(boxy.extents)
    round_fill = round_.volume / np.prod(round_.extents)
    assert boxy_fill > round_fill


def test_gear_teeth_change_the_volume():
    small = build_shape("involute_gear", {"teeth": 12}, resolution=64)
    large = build_shape("involute_gear", {"teeth": 24}, resolution=64)
    assert large.volume > small.volume


def test_lathe_accepts_a_custom_profile():
    profile = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
    mesh = build_shape("lathe", {"profile": profile}, resolution=64)
    assert mesh.is_watertight
    assert 0.9 < max(mesh.extents) < 1.3


def test_torus_knot_p_and_q():
    trefoil = build_shape("torus_knot", {"p": 2, "q": 3}, resolution=48)
    other = build_shape("torus_knot", {"p": 3, "q": 4}, resolution=48)
    assert not np.allclose(trefoil.extents, other.extents)


def test_seed_changes_spherical_harmonic():
    a = build_shape("spherical_harmonic", {"seed": 1}, resolution=40)
    b = build_shape("spherical_harmonic", {"seed": 2}, resolution=40)
    assert not np.allclose(a.vertices, b.vertices)


def test_open_shapes_are_marked_open():
    for name in ("mobius", "helicoid", "catenoid"):
        assert SHAPES[name].closed is False
        assert build_shape(name, resolution=RES).is_watertight is False


# --- validation integration ---------------------------------------------
def test_closed_shapes_pass_the_print_profile():
    for name in ("supershape", "torus_knot", "spring", "involute_gear", "lathe"):
        mesh = build_shape(name, resolution=64)
        report = validate.validate(mesh, profile="print", spec=ModelSpec(purpose="print"))
        assert report.ok, f"{name}: {report.to_dict()['failed']}"


def test_open_shape_fails_print_but_passes_display():
    mesh = build_shape("mobius", resolution=RES)
    assert validate.validate(mesh, profile="print").ok is False
    assert validate.validate(mesh, profile="web").ok is True


# --- formats -------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["stl", "obj", "ply", "glb", "gltf"])
def test_shape_exports_to_every_format(tmp_path, fmt):
    mesh = build_shape("torus_knot", resolution=32)
    out = meshio.export(mesh, tmp_path / f"knot.{fmt}")
    assert out.exists() and out.stat().st_size > 0
    if fmt != "gltf":  # gltf writes sidecar buffers; load from the main file
        assert len(meshio.as_mesh(meshio.load(out)).faces) > 0


# --- MCP tools -----------------------------------------------------------
def test_list_shapes_tool():
    out = server.list_shapes()
    assert out["ok"] and out["count"] >= 15
    assert any(s["name"] == "supershape" for s in out["shapes"])


def test_create_shape_tool_end_to_end():
    out = server.create_shape(shape="torus_knot", params={"p": 2, "q": 3},
                              resolution=48, name="tool-knot",
                              render_previews=False, make_viewer=False)
    assert out["ok"], out
    assert out["card"]["watertight"] is True
    assert out["validation"]["ok"] is True
    from pathlib import Path

    assert Path(out["path"]).exists()


def test_create_shape_tool_reports_an_unknown_shape():
    out = server.create_shape(shape="nope")
    assert out["ok"] is False
    assert "list_shapes" in out["hint"]


def test_create_shape_tool_exports_a_requested_format():
    out = server.create_shape(shape="twisted_prism", resolution=32, name="tool-prism",
                              export_format="obj", render_previews=False,
                              make_viewer=False)
    assert out["ok"]
    assert "obj" in out["extra_formats"]
