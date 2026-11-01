"""Procedural planets and multi-model scenes.

These tests exist because a planet generator is easy to write and hard to get
right: seamless noise, crater rims that read as geology, UVs that line up with
the baked maps, and determinism so a website hero image is reproducible.
"""

import hashlib

import numpy as np
import pytest

from forge3d import backends, meshio, server
from forge3d.geometry.planets import (
    PlanetParams,
    attach_pbr,
    bake_textures,
    generate_planet,
    generate_scene,
)
from forge3d.spec import ModelSpec
from forge3d.viewer import build_scene_viewer

SMALL = {"radius": 10.0, "subdivisions": 3, "texture_resolution": 0}


def _hash(mesh) -> str:
    return hashlib.sha1(np.round(mesh.vertices, 6).tobytes()).hexdigest()


# --- determinism ---------------------------------------------------------
def test_same_seed_gives_the_same_planet():
    a, _ = generate_planet(PlanetParams(seed=42, **SMALL))
    b, _ = generate_planet(PlanetParams(seed=42, **SMALL))
    assert _hash(a) == _hash(b)


def test_different_seeds_give_different_planets():
    a, _ = generate_planet(PlanetParams(seed=1, **SMALL))
    b, _ = generate_planet(PlanetParams(seed=2, **SMALL))
    assert _hash(a) != _hash(b)


def test_radius_is_respected():
    """`extents` is the bounding-box size — a diameter, so ~2x the radius."""
    mesh, info = generate_planet(PlanetParams(radius=50.0, seed=3, subdivisions=3))
    assert info["radius"] == 50.0
    assert 90 < max(mesh.extents) < 112


# --- geometry quality ----------------------------------------------------
def test_planet_is_watertight_with_uvs():
    mesh, _ = generate_planet(PlanetParams(seed=5, **SMALL))
    assert mesh.is_watertight
    assert mesh.visual.uv is not None
    assert len(mesh.visual.uv) == len(mesh.vertices)


def test_relief_stays_inside_the_radius():
    """A displacement larger than the radius turns the planet inside out."""
    for seed in range(6):
        _mesh, info = generate_planet(
            PlanetParams(seed=seed, canyon=True, canyon_depth=0.2, **SMALL))
        low, high = info["relief_range"]
        assert low > -0.4 and high < 0.4, f"seed {seed} relief {low}..{high}"


def test_craters_change_the_surface():
    smooth, _ = generate_planet(PlanetParams(seed=9, crater_density=0.0, **SMALL))
    cratered, _ = generate_planet(PlanetParams(seed=9, crater_density=1.0, **SMALL))
    assert not np.allclose(smooth.vertices, cratered.vertices)


def test_canyon_carves_below_the_surface():
    plain, _ = generate_planet(PlanetParams(seed=11, **SMALL))
    carved, info = generate_planet(
        PlanetParams(seed=11, canyon=True, canyon_depth=0.08, **SMALL))
    assert info["relief_range"][0] < -0.01
    # Compare radial distance, not an axis: the canyon follows a random great
    # circle, so its lowest point is not necessarily the lowest X.
    plain_min = np.linalg.norm(plain.vertices, axis=1).min()
    carved_min = np.linalg.norm(carved.vertices, axis=1).min()
    assert carved_min < plain_min


def test_vertex_colours_are_present_and_varied():
    mesh, _ = generate_planet(PlanetParams(seed=13, **SMALL))
    colours = mesh.visual.vertex_colors
    assert colours is not None and len(colours) == len(mesh.vertices)
    assert len(np.unique(colours[:, :3], axis=0)) > 20, "surface is a flat colour"


def test_moon_palette_differs_from_mars():
    mars, _ = generate_planet(PlanetParams(seed=17, palette="mars", **SMALL))
    moon, _ = generate_planet(PlanetParams(seed=17, palette="moon", **SMALL))
    mars_mean = mars.visual.vertex_colors[:, 0].mean()
    moon_mean = moon.visual.vertex_colors[:, 0].mean()
    assert mars_mean > moon_mean, "mars should be redder than the moon"


def test_subdivisions_control_density():
    low, i_low = generate_planet(PlanetParams(seed=1, radius=5, subdivisions=2))
    high, i_high = generate_planet(PlanetParams(seed=1, radius=5, subdivisions=4))
    assert i_high["faces"] > i_low["faces"] * 4


# --- textures ------------------------------------------------------------
def test_textures_bake_and_are_correctly_sized(tmp_path):
    from PIL import Image

    params = PlanetParams(name="tex", radius=10, subdivisions=3, seed=3,
                          texture_resolution=128)
    paths = bake_textures(params, tmp_path)
    assert set(paths) == {"albedo", "metallic_roughness", "normal"}
    for path in paths.values():
        with Image.open(path) as img:
            assert img.size == (128, 128)


def test_metallic_roughness_is_gltf_packed(tmp_path):
    """glTF wants G=roughness, B=metallic in one map. A separate 'roughness'
    image is silently dropped by exporters — that was a real bug here."""
    from PIL import Image

    params = PlanetParams(name="mr", radius=10, subdivisions=3, seed=4,
                          texture_resolution=64)
    paths = bake_textures(params, tmp_path)
    with Image.open(paths["metallic_roughness"]) as img:
        arr = np.asarray(img.convert("RGB"))
    assert arr[:, :, 2].max() == 0, "metallic channel must be 0 for rock"
    assert arr[:, :, 1].min() < arr[:, :, 1].max(), "roughness channel is constant"


def test_pbr_material_survives_glb_round_trip(tmp_path):
    mesh, info = generate_planet(
        PlanetParams(name="pbr", radius=10, subdivisions=3, seed=6,
                     texture_resolution=64), out_dir=tmp_path)
    attach_pbr(mesh, info["textures"])
    glb = tmp_path / "pbr.glb"
    mesh.export(str(glb))

    reloaded = meshio.as_mesh(meshio.load(glb))
    material = reloaded.visual.material
    assert material.baseColorTexture is not None
    assert material.metallicRoughnessTexture is not None
    assert material.normalTexture is not None


def test_texture_resolution_zero_skips_baking(tmp_path):
    _mesh, info = generate_planet(PlanetParams(name="notex", **SMALL), out_dir=tmp_path)
    assert info["textures"] == {}


# --- scenes --------------------------------------------------------------
def test_scene_places_every_planet():
    scene, infos = generate_scene([
        {"name": "a", "radius": 8, "seed": 1, "subdivisions": 2, "position": [0, 0, 0]},
        {"name": "b", "radius": 4, "seed": 2, "subdivisions": 2, "position": [30, 5, 0]},
    ])
    assert len(scene.geometry) == 2
    assert [i["name"] for i in infos] == ["a", "b"]
    assert infos[1]["position"] == [30, 5, 0]


def test_scene_scale_multiplies_radius():
    _scene, infos = generate_scene([
        {"name": "s", "radius": 10, "seed": 1, "subdivisions": 2, "scale": 2.5},
    ])
    assert infos[0]["radius"] == pytest.approx(25.0)


# --- scene viewer --------------------------------------------------------
def test_scene_viewer_embeds_every_model(tmp_path):
    paths = []
    for i in range(3):
        mesh, _ = generate_planet(PlanetParams(name=f"p{i}", radius=5, seed=i, subdivisions=2))
        p = tmp_path / f"p{i}.glb"
        mesh.export(str(p))
        paths.append({"path": str(p), "name": f"p{i}", "position": [i * 12, 0, 0]})

    html = build_scene_viewer(paths, tmp_path / "scene.html", title="Three Worlds")
    text = html.read_text(encoding="utf-8")
    assert text.count("data:model/gltf-binary") == 3
    assert "Three Worlds" in text
    for placeholder in ("__SCENE_JSON__", "__THREE_BASE__", "__TITLE__", "__META_JSON__"):
        assert placeholder not in text


def test_scene_viewer_escapes_the_title(tmp_path):
    mesh, _ = generate_planet(PlanetParams(name="x", radius=5, seed=1, subdivisions=2))
    p = tmp_path / "x.glb"
    mesh.export(str(p))
    html = build_scene_viewer([{"path": str(p)}], tmp_path / "s.html",
                              title="</script><img src=x>")
    text = html.read_text(encoding="utf-8")
    assert "</script><img" not in text
    assert "&lt;/script&gt;" in text


def test_scene_viewer_rejects_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_scene_viewer([{"path": str(tmp_path / "nope.glb")}], tmp_path / "s.html")


# --- routing: never silently spend money ---------------------------------
def test_local_mode_never_returns_a_paid_provider(monkeypatch):
    monkeypatch.setenv("FORGE3D_HOSTED_URL", "http://127.0.0.1:1")
    from forge3d import config

    config._settings = None
    try:
        spec = ModelSpec(references=["https://example.com/a.png"], mode="local", purpose="web")
        assert backends.resolve_auto(spec) != "hosted"
    finally:
        monkeypatch.delenv("FORGE3D_HOSTED_URL", raising=False)
        config._settings = None


def test_cloud_mode_returns_the_hosted_backend():
    assert backends.resolve_auto(ModelSpec(mode="cloud")) == "hosted"


def test_auto_mode_is_unchanged_by_the_mode_field():
    assert backends.resolve_auto(ModelSpec(mode="auto")) == "procedural"


def test_unknown_mode_falls_back_to_auto():
    assert ModelSpec(mode="turbo").mode == "auto"


# --- MCP tools -----------------------------------------------------------
def test_create_planet_tool_end_to_end():
    out = server.create_planet(name="tool-planet", radius=20, seed=5, subdivisions=3,
                               texture_resolution=0, render_previews=False,
                               make_viewer=False)
    assert out["ok"], out
    assert out["planet"]["faces"] > 0
    from pathlib import Path

    assert Path(out["path"]).exists()


def test_make_scene_viewer_tool():
    a = server.create_planet(name="scene-a", radius=10, seed=1, subdivisions=2,
                             texture_resolution=0, render_previews=False, make_viewer=False)
    b = server.create_planet(name="scene-b", radius=6, seed=2, subdivisions=2,
                             texture_resolution=0, render_previews=False, make_viewer=False)
    out = server.make_scene_viewer(
        items=[{"path": a["path"], "position": [0, 0, 0]},
               {"path": b["path"], "position": [40, 10, 0]}],
        title="Tool Scene")
    assert out["ok"], out
    assert out["bodies"] == 2


def test_make_scene_viewer_rejects_empty_input():
    out = server.make_scene_viewer(items=[])
    assert out["ok"] is False
    assert "hint" in out
