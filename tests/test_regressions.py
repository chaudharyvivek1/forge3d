"""Regressions for bugs found in the v0.1.0 audit.

Each test names the failure it locks down, because the whole point is that these
were shipped bugs, not hypotheticals.
"""

import json
import threading
from pathlib import Path

import pytest
import trimesh

from forge3d import meshio, server, viewer
from forge3d.spec import ModelSpec

CUBE = "import trimesh\ndef build(): return trimesh.creation.box(extents=[10,10,10])"


# --- 1. procedural backend inside a worker thread -------------------------
def test_procedural_backend_works_in_a_worker_thread():
    """v0.1.0 used signal.alarm, which raises off the main thread.

    MCP servers execute synchronous tools in a threadpool, so the default
    backend failed in the exact environment it exists for.
    """
    box = {}

    def worker():
        try:
            r = server.backends.get_backend("procedural").generate(
                ModelSpec(name="threaded"), Path("/tmp/forge3d-thread-test"), source=CUBE
            )
            box["ok"], box["error"] = r.ok, r.error
        except Exception as exc:  # noqa: BLE001
            box["raised"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "raised" not in box, box
    assert box["ok"] is True, box


def test_create_model_survives_a_worker_thread(tmp_path):
    box = {}

    def worker():
        box.update(server.create_model(name="threaded-pipeline", backend="procedural",
                                       source=CUBE, render_previews=False, make_viewer=False))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert box.get("ok") is True, box


# --- 2. multi-file exports (.gltf) ---------------------------------------
def test_gltf_export_writes_sidecar_buffers(tmp_path, cube):
    """trimesh returns a dict of filename -> payload for .gltf.

    v0.1.0 passed that dict to `write_bytes`, raising
    `TypeError: memoryview: a bytes-like object is required, not 'dict'`.
    """
    out = meshio.export(cube, tmp_path / "m.gltf")
    assert out.exists() and out.suffix == ".gltf"
    assert out.stat().st_size > 0
    assert json.loads(out.read_text(encoding="utf-8"))["asset"]


def test_gltf_output_can_be_read_back(tmp_path, cube):
    out = meshio.export(cube, tmp_path / "round.gltf")
    again = meshio.load(out)
    assert len(meshio.as_mesh(again).faces) > 0


def test_convert_model_to_gltf_via_the_tool(tmp_path, cube):
    src = tmp_path / "c.glb"
    cube.export(str(src))
    result = server.convert_model(str(src), fmt="gltf")
    assert result["ok"], result
    assert Path(result["path"]).exists()


@pytest.mark.parametrize("fmt", ["stl", "obj", "ply", "off", "glb"])
def test_all_advertised_formats_actually_write(tmp_path, cube, fmt):
    out = meshio.export(cube, tmp_path / f"x.{fmt}")
    assert out.exists() and out.stat().st_size > 0


def test_dae_without_pycollada_gives_a_usable_error(tmp_path, cube):
    """A raw ImportError told the agent nothing actionable."""
    try:
        import pycollada  # noqa: F401
    except Exception:
        with pytest.raises(meshio.MeshError) as exc:
            meshio.export(cube, tmp_path / "x.dae")
        assert "pycollada" in str(exc.value) or "optional" in str(exc.value)
    else:
        pytest.skip("pycollada installed")


def test_unknown_format_names_the_alternatives(tmp_path, cube):
    with pytest.raises(meshio.MeshError, match="supported"):
        meshio.export(cube, tmp_path / "x.step")


# --- 3. viewer escaping --------------------------------------------------
def test_viewer_title_cannot_break_out_of_the_page(tmp_path, cube):
    """`</script>` in a title used to terminate the script block — XSS vector."""
    src = tmp_path / "m.glb"
    cube.export(str(src))
    out = viewer.build_viewer(src, tmp_path / "v.html", title="</script><img src=x onerror=alert(1)>")
    html = out.read_text(encoding="utf-8")
    assert "</script><img" not in html
    assert "&lt;/script&gt;" in html


def test_viewer_meta_json_cannot_break_the_script(tmp_path, cube):
    src = tmp_path / "m.glb"
    cube.export(str(src))
    spec = ModelSpec(name="</script>alert(1)", purpose="web")
    out = viewer.build_viewer(src, tmp_path / "v.html", spec=spec)
    html = out.read_text(encoding="utf-8")
    assert html.count("</script>") == html.count("<script")  # balanced, no injection


def test_viewer_three_url_is_configurable(tmp_path, cube, monkeypatch):
    """Air-gapped deployments need a local three.js mirror."""
    monkeypatch.setenv("FORGE3D_THREE_URL", "http://localhost:8080/three")
    src = tmp_path / "m.glb"
    cube.export(str(src))
    html = viewer.build_viewer(src, tmp_path / "v.html").read_text(encoding="utf-8")
    assert "http://localhost:8080/three/build/three.module.js" in html
    assert "unpkg.com" not in html


def test_viewer_defaults_to_the_public_cdn(tmp_path, cube, monkeypatch):
    monkeypatch.delenv("FORGE3D_THREE_URL", raising=False)
    src = tmp_path / "m.glb"
    cube.export(str(src))
    html = viewer.build_viewer(src, tmp_path / "v.html").read_text(encoding="utf-8")
    assert "unpkg.com/three" in html


def test_viewer_offline_failure_is_explained(tmp_path, cube):
    src = tmp_path / "m.glb"
    cube.export(str(src))
    html = viewer.build_viewer(src, tmp_path / "v.html").read_text(encoding="utf-8")
    assert "Could not load the 3D engine" in html


# --- 4. embed=False ------------------------------------------------------
def test_non_embedded_viewer_copies_the_mesh_beside_the_page(tmp_path, cube):
    """v0.1.0 linked a relative path it never wrote."""
    src = tmp_path / "src" / "m.glb"
    src.parent.mkdir()
    cube.export(str(src))
    out = viewer.build_viewer(src, tmp_path / "site" / "page.html", embed=False)
    assert (out.parent / "m.glb").exists()
    assert "data:model/gltf-binary" not in out.read_text(encoding="utf-8")


def test_embedded_viewer_needs_no_sidecar(tmp_path, cube):
    src = tmp_path / "m.glb"
    cube.export(str(src))
    out = viewer.build_viewer(src, tmp_path / "site" / "page.html")
    assert "data:model/gltf-binary;base64," in out.read_text(encoding="utf-8")


# --- 5. tool surface -----------------------------------------------------
def test_create_model_accepts_materials_and_seed(tmp_path):
    """Both were on ModelSpec but unreachable from the main tool."""
    result = server.create_model(
        name="mat-test", backend="procedural", source=CUBE,
        materials=["oak", "steel"], seed=7, render_previews=False, make_viewer=False,
    )
    assert result["ok"], result
    job = server.get_job(result["job_id"])["job"]
    assert job["spec"]["materials"] == ["oak", "steel"]
    assert job["spec"]["seed"] == 7


def test_doctor_tool_reports_the_environment():
    out = server.doctor()
    assert out["ok"] is True
    assert out["report"]["engines"]["procedural"]["available"] is True
    assert isinstance(out["hints"], list)


def test_doctor_tool_output_stays_small():
    """It is a tool response — it must not flood a context window."""
    out = server.doctor()
    assert len(json.dumps(out)) < 8000


# --- 6. routing ----------------------------------------------------------
def test_scad_source_routes_to_openscad_without_the_module_keyword():
    from forge3d import backends

    src = "difference() { cube([10,10,10]); translate([0,0,5]) cylinder(h=20, d=4); }"
    assert backends.resolve_auto(ModelSpec(), source=src) == "openscad"


def test_python_using_a_cube_function_is_not_routed_to_openscad():
    from forge3d import backends

    src = "import trimesh\ndef build():\n    return trimesh.creation.box()"
    assert backends.resolve_auto(ModelSpec(), source=src) == "procedural"


def test_blender_source_wins_over_scad_tokens():
    from forge3d import backends

    src = "import bpy\nbpy.ops.mesh.primitive_cube_add()\n# cube( like text"
    assert backends.resolve_auto(ModelSpec(), source=src) == "blender"


# --- 7. mesh stats robustness -------------------------------------------
def test_stats_survive_a_mesh_with_no_scipy(monkeypatch, cube):
    """body_count walks the face graph; a missing scipy must not fail inspection."""
    import forge3d.meshio as m

    class Broken:
        @property
        def body_count(self):
            raise ModuleNotFoundError("No module named 'scipy'")

        def __getattr__(self, item):
            return getattr(cube, item)

    st = m.stats(Broken())
    assert st["body_count"] == -1
    assert st["faces"] == len(cube.faces)


def test_empty_mesh_stats_are_safe():
    st = meshio.stats(trimesh.Trimesh())
    assert st["faces"] == 0 and st["extents"] == [0.0, 0.0, 0.0]
