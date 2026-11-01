"""Deterministic procedural planets — the reusable generator, not a Mars one-off.

The transcript's Mars scene needs four rocky planets with craters, a canyon, and
a surface that survives close inspection on a website. The `procedural` backend
can express that as raw trimesh code, but every project then reinvents the same
noise, crater and UV work. This module is that work, done once:

    from forge3d.geometry.planets import PlanetParams, generate_planet
    mesh, info = generate_planet(PlanetParams(radius=100, seed=7, canyon=True))

Everything is seeded and deterministic: the same params give the same mesh, the
same textures, byte for byte. No GPU, no external binary, no network.

Design notes worth knowing:

* **Noise is sampled in 3D on the unit sphere**, not in UV space. That makes the
  displacement seamless across the whole surface — the classic "visible seam
  down the back of the planet" bug simply cannot happen.
* **Craters are analytic, not noise.** A hash-seeded set of impact sites with a
  bowl-and-rim profile reads as geology; fBm alone reads as clouds.
* **UVs are spherical**, which is the one case where naive UV mapping is correct
  rather than a compromise, so the baked maps line up with the geometry exactly.
* **Textures are baked from the same fields as the geometry**, so the albedo and
  the silhouette always agree. This is what closes most of the "no PBR" gap
  without needing Blender.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

MARS = {
    "low": (0.28, 0.11, 0.07),
    "mid": (0.60, 0.27, 0.15),
    "high": (0.82, 0.48, 0.30),
}

MOON = {
    "low": (0.22, 0.22, 0.24),
    "mid": (0.45, 0.45, 0.47),
    "high": (0.72, 0.72, 0.74),
}


@dataclass
class PlanetParams:
    """Everything that decides what the planet looks like.

    radius              world units
    seed                any int; the same seed always gives the same planet
    subdivisions        icosphere level: 4 ≈ 2.5k verts, 5 ≈ 10k, 6 ≈ 41k
    terrain_amplitude   relief height as a fraction of radius
    roughness           noise frequency multiplier (higher = busier)
    crater_density      0..1, how many impact sites
    crater_size_range   min/max crater radius as a fraction of the planet radius
    canyon              carve a great-circle canyon
    canyon_depth        canyon depth as a fraction of radius
    canyon_width        angular half-width, in radians
    palette             "mars" | "moon" | dict(low/mid/high) RGB triples
    texture_resolution  bake albedo/roughness/normal at this size; 0 disables
    """

    radius: float = 100.0
    seed: int = 0
    subdivisions: int = 5
    terrain_amplitude: float = 0.035
    roughness: float = 1.0
    crater_density: float = 0.55
    crater_size_range: tuple[float, float] = (0.015, 0.09)
    canyon: bool = False
    canyon_depth: float = 0.05
    canyon_width: float = 0.10
    palette: object = "mars"
    texture_resolution: int = 0
    name: str = "planet"

    def colors(self) -> dict:
        if isinstance(self.palette, dict):
            return self.palette
        if str(self.palette).lower() == "moon":
            return MOON
        return MARS


# ---------------------------------------------------------------------------
# deterministic value noise
# ---------------------------------------------------------------------------
def _hash3(ix: np.ndarray, iy: np.ndarray, iz: np.ndarray, seed: int) -> np.ndarray:
    """Three pseudo-random floats per lattice cell, from integer coords.

    A hash rather than a stored gradient table: it is stateless, so any seed
    works and there is nothing to keep in sync between geometry and textures.
    """
    h = (ix.astype(np.int64) * 374761393
         + iy.astype(np.int64) * 668265263
         + iz.astype(np.int64) * 1274126177
         + np.int64(seed) * 2654435761) & 0xFFFFFFFF
    out = np.empty(h.shape + (3,), dtype=np.float64)
    for k in range(3):
        h = (h ^ (h >> 13)) * np.int64(1274126177) & 0xFFFFFFFF
        out[..., k] = (h % 10007) / 10007.0 - 0.5
    return out


def _smoothstep(t: np.ndarray) -> np.ndarray:
    return t * t * (3.0 - 2.0 * t)


def value_noise(points: np.ndarray, seed: int) -> np.ndarray:
    """Trilinear value noise in [-0.5, 0.5], vectorised over N points."""
    p = np.asarray(points, dtype=np.float64)
    base = np.floor(p)
    f = _smoothstep(p - base)
    i = base.astype(np.int64)

    total = np.zeros(len(p), dtype=np.float64)
    for dx in (0, 1):
        wx = f[:, 0] if dx else 1.0 - f[:, 0]
        for dy in (0, 1):
            wy = f[:, 1] if dy else 1.0 - f[:, 1]
            for dz in (0, 1):
                wz = f[:, 2] if dz else 1.0 - f[:, 2]
                g = _hash3(i[:, 0] + dx, i[:, 1] + dy, i[:, 2] + dz, seed)
                weight = wx * wy * wz
                total += weight * (g[:, 0] * (p[:, 0] - (base[:, 0] + dx))
                                   + g[:, 1] * (p[:, 1] - (base[:, 1] + dy))
                                   + g[:, 2] * (p[:, 2] - (base[:, 2] + dz)))
    return np.clip(total, -0.5, 0.5)


def fbm(points: np.ndarray, seed: int, octaves: int = 5,
        lacunarity: float = 2.0, gain: float = 0.5) -> np.ndarray:
    """Fractional Brownian motion — the terrain backbone."""
    total = np.zeros(len(points), dtype=np.float64)
    amplitude = 1.0
    frequency = 1.0
    norm = 0.0
    for o in range(octaves):
        total += amplitude * value_noise(points * frequency, seed + o * 101)
        norm += amplitude
        amplitude *= gain
        frequency *= lacunarity
    return total / max(norm, 1e-9)


def ridged(points: np.ndarray, seed: int, octaves: int = 4) -> np.ndarray:
    """Ridged noise: sharp crests, good for canyon walls and old crater rims."""
    base = fbm(points, seed, octaves=octaves)
    return 1.0 - np.abs(base) * 2.0


# ---------------------------------------------------------------------------
# surface fields
# ---------------------------------------------------------------------------
def _crater_field(dirs: np.ndarray, params: PlanetParams) -> np.ndarray:
    """Bowl-and-rim profile from a seeded set of impact sites.

    A crater is not a dent: real ones have a raised rim from ejected material.
    Without the rim the surface reads as noise with dimples, which is the tell
    that a planet was generated rather than authored.
    """
    if params.crater_density <= 0:
        return np.zeros(len(dirs), dtype=np.float64)

    rng = np.random.default_rng(params.seed + 977)
    count = int(28 * params.crater_density) + 4
    lo, hi = params.crater_size_range

    # Fibonacci-sphere sites, then jittered by the rng so density is even but
    # not obviously regular.
    idx = np.arange(count) + 0.5
    phi = np.arccos(1 - 2 * idx / count)
    theta = math.pi * (1 + 5 ** 0.5) * idx
    centres = np.column_stack([
        np.cos(theta) * np.sin(phi),
        np.sin(theta) * np.sin(phi),
        np.cos(phi),
    ])
    centres += rng.normal(0, 0.06, centres.shape)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    radii = rng.uniform(lo, hi, count)

    field_ = np.zeros(len(dirs), dtype=np.float64)
    for centre, r in zip(centres, radii, strict=True):
        cos_d = np.clip(dirs @ centre, -1.0, 1.0)
        d = np.arccos(cos_d)                     # angular distance
        x = d / r
        bowl = np.where(x < 1.0, -(1.0 - x ** 2) * 0.75, 0.0)
        rim = np.where((x >= 0.85) & (x < 1.35), np.exp(-((x - 1.1) ** 2) / 0.006) * 0.28, 0.0)
        field_ += bowl + rim
    return field_


def _canyon_field(dirs: np.ndarray, params: PlanetParams) -> np.ndarray:
    """A great-circle rift with noisy walls — Valles Marineris in one function."""
    if not params.canyon:
        return np.zeros(len(dirs), dtype=np.float64)

    rng = np.random.default_rng(params.seed + 31337)
    normal = rng.normal(size=3)
    normal /= np.linalg.norm(normal)

    distance = np.abs(dirs @ normal)                       # 0 on the great circle
    width = params.canyon_width * (1.0 + 0.45 * fbm(dirs * 3.0, params.seed + 5, octaves=3))
    inside = np.clip(1.0 - distance / np.maximum(width, 1e-6), 0.0, 1.0)
    walls = 0.7 + 0.6 * ridged(dirs * 6.0, params.seed + 11, octaves=3)
    return -inside * walls


def _height_field(dirs: np.ndarray, params: PlanetParams) -> np.ndarray:
    """Total displacement as a fraction of radius, before scaling."""
    base = fbm(dirs * (1.6 * params.roughness), params.seed, octaves=6)
    detail = 0.35 * fbm(dirs * (7.0 * params.roughness), params.seed + 41, octaves=4)
    relief = (base + detail) * params.terrain_amplitude * 2.2

    relief = relief + _crater_field(dirs, params) * params.terrain_amplitude * 2.4
    # canyon_depth is a fraction of the radius, like terrain_amplitude — dividing
    # by terrain_amplitude here (an earlier version did) produced a displacement
    # larger than the planet and turned it inside out.
    relief = relief + _canyon_field(dirs, params) * params.canyon_depth
    # Never let relief exceed the radius: a self-intersecting planet is not a
    # planet, and it would fail validation downstream anyway.
    return np.clip(relief, -0.35, 0.35)


# ---------------------------------------------------------------------------
# mesh
# ---------------------------------------------------------------------------
def _spherical_uv(dirs: np.ndarray) -> np.ndarray:
    u = (np.arctan2(dirs[:, 2], dirs[:, 0]) / (2 * math.pi)) + 0.5
    v = (np.arcsin(np.clip(dirs[:, 1], -1.0, 1.0)) / math.pi) + 0.5
    return np.column_stack([u, v])


def _lerp_palette(t: np.ndarray, colors: dict) -> np.ndarray:
    low = np.array(colors["low"], dtype=np.float64)
    mid = np.array(colors["mid"], dtype=np.float64)
    high = np.array(colors["high"], dtype=np.float64)
    t = np.clip(t, 0.0, 1.0)[:, None]
    lower = low + (mid - low) * (t / 0.5)
    upper = mid + (high - mid) * ((t - 0.5) / 0.5)
    return np.where(t < 0.5, lower, upper)


def generate_planet(params: PlanetParams, out_dir: str | Path | None = None):
    """Build one planet. Returns (mesh, info).

    `info` carries the deterministic facts a caller needs to record: seed, face
    count, the height range actually produced, and any texture paths.
    """
    mesh = trimesh.creation.icosphere(subdivisions=max(2, min(7, params.subdivisions)),
                                     radius=1.0)
    dirs = mesh.vertices / np.linalg.norm(mesh.vertices, axis=1, keepdims=True)

    height = _height_field(dirs, params)
    radii = params.radius * (1.0 + height)
    mesh.vertices = dirs * radii[:, None]

    span = float(height.max() - height.min()) or 1.0
    normalised = (height - height.min()) / span

    mesh.visual = trimesh.visual.TextureVisuals(uv=_spherical_uv(dirs))
    mesh.visual.vertex_colors = np.column_stack([
        (_lerp_palette(normalised, params.colors()) * 255).astype(np.uint8),
        np.full(len(dirs), 255, dtype=np.uint8),
    ])

    info = {
        "name": params.name,
        "seed": params.seed,
        "faces": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "radius": params.radius,
        "relief_range": [round(float(height.min()), 4), round(float(height.max()), 4)],
        "craters": bool(params.crater_density > 0),
        "canyon": bool(params.canyon),
        "textures": {},
    }

    if params.texture_resolution and out_dir is not None:
        info["textures"] = bake_textures(params, Path(out_dir))

    mesh.merge_vertices()
    return mesh, info


# ---------------------------------------------------------------------------
# texture baking
# ---------------------------------------------------------------------------
def _sample_field(res: int, params: PlanetParams) -> tuple[np.ndarray, np.ndarray]:
    """Sample the height field on a lat-long grid, matching the UV layout."""
    u = (np.arange(res) + 0.5) / res
    v = (np.arange(res) + 0.5) / res
    uu, vv = np.meshgrid(u, v)

    lon = (uu - 0.5) * 2 * math.pi
    lat = (vv - 0.5) * math.pi
    dirs = np.column_stack([
        (np.cos(lat) * np.cos(lon)).ravel(),
        np.sin(lat).ravel(),
        (np.cos(lat) * np.sin(lon)).ravel(),
    ])
    return _height_field(dirs, params).reshape(res, res), dirs


def bake_textures(params: PlanetParams, out_dir: Path) -> dict:
    """Write albedo, roughness and normal maps as PNGs.

    These are baked from the same fields the geometry uses, so the colour, the
    bumps and the silhouette agree. It is not a hand-authored PBR set — there is
    no occlusion pass and no detail normal — but it is a real, correctly-UV'd
    map set rather than a flat colour.
    """
    from PIL import Image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = int(max(64, min(2048, params.texture_resolution)))

    height, _dirs = _sample_field(res, params)
    span = float(height.max() - height.min()) or 1.0
    normalised = (height - height.min()) / span

    albedo = (_lerp_palette(normalised.ravel(), params.colors())
              .reshape(res, res, 3) * 255).astype(np.uint8)
    # Dust settles in the lows; a touch of grain stops large flats reading as plastic.
    grain = (np.random.default_rng(params.seed + 7).normal(0, 6, (res, res, 1))).astype(np.float64)
    albedo = np.clip(albedo.astype(np.float64) + grain, 0, 255).astype(np.uint8)

    # glTF packs metallic+roughness into one map: G = roughness, B = metallic.
    # A standalone "roughness" image is silently dropped by exporters, which is
    # how a PBR set ends up looking flat in a viewer while the files look right.
    roughness = np.clip(0.95 - normalised * 0.35, 0.25, 1.0)
    metallic = np.zeros_like(roughness)
    mr = np.stack([np.ones_like(roughness), roughness, metallic], axis=-1)
    rough_rgb = (np.clip(mr, 0, 1) * 255).astype(np.uint8)

    # Normal map from the height gradient, in tangent space.
    gy, gx = np.gradient(height)
    strength = 3.0 * (params.terrain_amplitude / 0.035)
    nx, ny, nz = -gx * strength * res / 64.0, -gy * strength * res / 64.0, np.ones_like(height)
    length = np.sqrt(nx ** 2 + ny ** 2 + nz ** 2)
    normal_rgb = np.stack([nx / length, ny / length, nz / length], axis=-1)
    normal_rgb = ((normal_rgb * 0.5 + 0.5) * 255).astype(np.uint8)

    paths = {}
    for name, array in (("albedo", albedo), ("metallic_roughness", rough_rgb),
                        ("normal", normal_rgb)):
        p = out_dir / f"{params.name}_{name}.png"
        Image.fromarray(array).save(p)
        paths[name] = str(p)
    return paths


def attach_pbr(mesh: trimesh.Trimesh, textures: dict) -> trimesh.Trimesh:
    """Wrap the mesh in a PBR material that survives GLB export."""
    from PIL import Image
    from trimesh.visual.material import PBRMaterial

    material = PBRMaterial(
        name="planet_surface",
        baseColorTexture=Image.open(textures["albedo"]) if "albedo" in textures else None,
        metallicFactor=0.0,
        roughnessFactor=1.0,
    )
    if "metallic_roughness" in textures:
        material.metallicRoughnessTexture = Image.open(textures["metallic_roughness"])
    if "normal" in textures:
        material.normalTexture = Image.open(textures["normal"])
    mesh.visual = trimesh.visual.TextureVisuals(uv=mesh.visual.uv, material=material)
    return mesh


# ---------------------------------------------------------------------------
# scene
# ---------------------------------------------------------------------------
def generate_scene(specs: list[dict]) -> tuple[trimesh.Scene, list[dict]]:
    """Build a multi-planet scene.

    `specs` is a list of dicts: PlanetParams fields plus optional
    `position` and `scale` for placement. Returns the scene and per-planet info.
    """
    scene = trimesh.Scene()
    infos = []
    for entry in specs:
        entry = dict(entry)
        position = entry.pop("position", [0.0, 0.0, 0.0])
        scale = float(entry.pop("scale", 1.0))
        entry.pop("texture_resolution", None)

        allowed = set(PlanetParams.__dataclass_fields__)
        params = PlanetParams(**{k: v for k, v in entry.items() if k in allowed})
        params.radius = params.radius * scale

        mesh, info = generate_planet(params)
        matrix = trimesh.transformations.translation_matrix(position)
        scene.add_geometry(mesh, node_name=params.name, transform=matrix)
        info["position"] = list(position)
        infos.append(info)
    return scene, infos
