"""Image-to-3D: real geometry derived from real pixels.

These tests build their own images so nothing depends on a fixture file being
committed, and they assert on geometry properties (watertight, volume, hole
preservation) rather than on "it produced something".
"""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge3d import backends, meshio, validate
from forge3d.backends.image_backend import ImageBackend, triangle_estimate
from forge3d.spec import ModelSpec


@pytest.fixture
def gradient_png(tmp_path):
    arr = np.tile(np.linspace(0, 255, 128), (128, 1)).astype("uint8")
    p = tmp_path / "gradient.png"
    Image.fromarray(arr, "L").convert("RGBA").save(p)
    return p


@pytest.fixture
def ring_png(tmp_path):
    """A disc with a hole — the case that proves holes survive extrusion."""
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((16, 16, 112, 112), fill=(255, 60, 60, 255))
    draw.ellipse((48, 48, 80, 80), fill=(0, 0, 0, 0))
    p = tmp_path / "ring.png"
    img.save(p)
    return p


@pytest.fixture
def solid_square_png(tmp_path):
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    p = tmp_path / "square.png"
    img.save(p)
    return p


# --- registration and routing -------------------------------------------
def test_image_backend_is_registered():
    assert "image" in {b["name"] for b in backends.list_backends()}


def test_image_backend_declares_honest_limitations():
    entry = next(b for b in backends.list_backends() if b["name"] == "image")
    caps = entry["capabilities"]
    assert "reconstructing hidden geometry" in caps["weak_for"]
    assert caps["deterministic"] is True


def test_local_image_routes_to_the_image_backend(ring_png):
    picked = backends.resolve_auto(ModelSpec(references=[str(ring_png)]))
    assert picked == "image"


def test_url_reference_does_not_route_to_image_backend():
    picked = backends.resolve_auto(ModelSpec(references=["https://example.com/a.png"]))
    assert picked != "image"


# --- relief --------------------------------------------------------------
def test_relief_is_a_watertight_solid(gradient_png):
    spec = ModelSpec(name="relief", purpose="print", dimensions={"x": 80, "y": 80, "z": 10})
    result = ImageBackend().generate(spec, gradient_png.parent / "out", source=str(gradient_png),
                                     mode="relief", grid=40)
    assert result.ok, result.error
    mesh = meshio.load(result.path)
    assert len(meshio.as_mesh(mesh).faces) > 100
    assert meshio.as_mesh(mesh).is_watertight
    assert meshio.as_mesh(mesh).volume > 0


def test_relief_respects_the_spec_dimensions(gradient_png):
    spec = ModelSpec(name="sized", purpose="print", dimensions={"x": 50, "y": 50, "z": 8})
    result = ImageBackend().generate(spec, gradient_png.parent / "o2", source=str(gradient_png),
                                     mode="relief", grid=36)
    assert result.ok
    extents = meshio.as_mesh(meshio.load(result.path)).extents
    assert abs(max(extents) - 50) < 0.5, f"expected ~50mm dominant axis, got {extents}"


def test_relief_invert_flips_the_height(gradient_png):
    spec = ModelSpec(name="inv", purpose="print", dimensions={"x": 40, "y": 40, "z": 6})
    plain = ImageBackend().generate(spec, gradient_png.parent / "a", source=str(gradient_png),
                                    mode="relief", grid=40)
    flipped = ImageBackend().generate(spec, gradient_png.parent / "b", source=str(gradient_png),
                                      mode="relief", grid=40, invert=True)
    assert plain.ok and flipped.ok
    a = meshio.as_mesh(meshio.load(plain.path))
    b = meshio.as_mesh(meshio.load(flipped.path))
    # Mirroring luminance must change the surface, not the bounding box.
    assert not np.allclose(a.vertices[:, 2], b.vertices[:, 2])
    assert np.allclose(a.extents, b.extents, atol=0.5)


# --- silhouette ----------------------------------------------------------
def test_silhouette_is_watertight(ring_png):
    spec = ModelSpec(name="ring", purpose="print", dimensions={"x": 60, "y": 60, "z": 6})
    result = ImageBackend().generate(spec, ring_png.parent / "ring-out", source=str(ring_png),
                                     mode="silhouette", grid=40)
    assert result.ok, result.error
    mesh = meshio.as_mesh(meshio.load(result.path))
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.volume > 0


def test_silhouette_preserves_holes(tmp_path):
    """A disc with a hole must not fill in — this is the point of the mode.

    Asserted against a control: the same disc without the hole. Comparing to a
    fixed volume fraction was brittle (the exact ring area depends on the
    threshold and the resampling), while "less material than the solid disc" is
    the property that actually matters.
    """
    spec = ModelSpec(name="ring", purpose="print", dimensions={"x": 60, "y": 60, "z": 5})

    ring = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring)
    d.ellipse((8, 8, 88, 88), fill=(255, 60, 60, 255))
    d.ellipse((34, 34, 62, 62), fill=(0, 0, 0, 0))
    ring_path = tmp_path / "ring.png"
    ring.save(ring_path)

    disc = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse((8, 8, 88, 88), fill=(255, 60, 60, 255))
    disc_path = tmp_path / "disc.png"
    disc.save(disc_path)

    ring_res = ImageBackend().generate(spec, tmp_path / "r", source=str(ring_path),
                                       mode="silhouette", grid=36, thickness=5.0)
    disc_res = ImageBackend().generate(spec, tmp_path / "d", source=str(disc_path),
                                       mode="silhouette", grid=36, thickness=5.0)
    assert ring_res.ok and disc_res.ok

    ring_vol = meshio.as_mesh(meshio.load(ring_res.path)).volume
    disc_vol = meshio.as_mesh(meshio.load(disc_res.path)).volume

    # Theory: the hole removes (inner_r / outer_r)^2 of the area. Drawn radii are
    # 14 and 40, so the ring should keep 1 - (14/40)^2 = 0.8775 of the volume.
    # Asserting against the arithmetic beats asserting "smaller than" — it catches
    # a hole that is the wrong size as well as one that vanished.
    expected = 1.0 - (14.0 / 40.0) ** 2
    ratio = ring_vol / disc_vol
    assert abs(ratio - expected) < 0.04, (
        f"hole geometry wrong: ring/disc volume {ratio:.3f}, expected ~{expected:.3f}"
    )


def test_silhouette_of_a_solid_square_matches_expected_volume(solid_square_png):
    """Geometry has to be *correct*, not merely present: 32x32 cells at 2mm."""
    spec = ModelSpec(name="sq", purpose="print", dimensions={"x": 32, "y": 32, "z": 2})
    result = ImageBackend().generate(spec, solid_square_png.parent / "sq", source=str(solid_square_png),
                                     mode="silhouette", grid=32, thickness=2.0)
    assert result.ok
    mesh = meshio.as_mesh(meshio.load(result.path))
    # Uniform white image -> fully filled mask -> area 32x32, thickness 2 (then
    # rescaled to a 32mm dominant axis, which it already is).
    assert mesh.volume == pytest.approx(32 * 32 * 2, rel=0.02)


# --- mode selection ------------------------------------------------------
def test_auto_picks_silhouette_for_a_transparent_cutout(ring_png):
    spec = ModelSpec(name="auto1", purpose="print")
    result = ImageBackend().generate(spec, ring_png.parent / "auto1", source=str(ring_png),
                                     mode="auto", grid=36)
    assert result.ok
    assert result.meta["mode"] == "silhouette"


def test_auto_picks_relief_for_a_photo_like_image(gradient_png):
    spec = ModelSpec(name="auto2", purpose="print")
    result = ImageBackend().generate(spec, gradient_png.parent / "auto2", source=str(gradient_png),
                                     mode="auto", grid=36)
    assert result.ok
    assert result.meta["mode"] == "relief"


# --- failure modes -------------------------------------------------------
def test_missing_image_gives_an_actionable_error(tmp_path):
    result = ImageBackend().generate(ModelSpec(name="none"), tmp_path, source=None)
    assert result.ok is False
    assert "needs an image" in result.error


def test_blank_image_reports_no_geometry(tmp_path):
    blank = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    p = tmp_path / "blank.png"
    blank.save(p)
    result = ImageBackend().generate(ModelSpec(name="blank"), tmp_path / "b", source=str(p),
                                     mode="silhouette")
    assert result.ok is False
    assert "no geometry" in result.error


def test_corrupt_image_is_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    result = ImageBackend().generate(ModelSpec(name="bad"), tmp_path / "c", source=str(bad))
    assert result.ok is False
    assert "could not read image" in result.error


def test_grid_is_clamped():
    """Unit-tested rather than by generating: a 512 grid is ~1M triangles and
    takes minutes, which is exactly why the clamp exists."""
    from forge3d.backends.image_backend import MAX_GRID, MIN_GRID, clamp_grid

    assert clamp_grid(5000) == MAX_GRID
    assert clamp_grid(1) == MIN_GRID
    assert clamp_grid(128) == 128


def test_poly_budget_shrinks_the_grid_to_fit():
    from forge3d.backends.image_backend import clamp_grid, triangle_estimate

    grid = clamp_grid(512, poly_budget=20_000)
    assert grid < 512
    assert triangle_estimate(grid) <= 20_000


def test_generation_respects_a_poly_budget(tmp_path, gradient_png):
    spec = ModelSpec(name="budgeted", purpose="game", poly_budget=8_000)
    result = ImageBackend().generate(spec, tmp_path / "b2", source=str(gradient_png),
                                     mode="relief", grid=400)
    assert result.ok
    assert result.meta["faces"] <= 8_000, f"{result.meta['faces']} faces exceeds the budget"
    assert result.meta["requested_grid"] == 400
    assert result.meta["grid"] < 400


# --- budgets -------------------------------------------------------------
def test_triangle_estimate_tracks_the_real_count(gradient_png):
    spec = ModelSpec(name="budget", purpose="print")
    result = ImageBackend().generate(spec, gradient_png.parent / "bud", source=str(gradient_png),
                                     mode="relief", grid=32)
    assert result.ok
    predicted = triangle_estimate(32)
    actual = result.meta["faces"]
    assert abs(predicted - actual) / actual < 0.25, f"estimate {predicted} vs actual {actual}"


def test_relief_passes_a_print_validation(tmp_path, gradient_png):
    spec = ModelSpec(name="valid", purpose="print", dimensions={"x": 60, "y": 60, "z": 8})
    result = ImageBackend().generate(spec, tmp_path / "v", source=str(gradient_png),
                                     mode="relief", grid=36)
    report = validate.validate(meshio.load(result.path), profile="print", spec=spec)
    assert report.ok, report.to_dict()
