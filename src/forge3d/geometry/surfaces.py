"""Parametric surfaces — geometry defined by an equation, not by hand.

The idea: an agent should not have to write noise code or vertex loops to get a
shape. It picks a family by name, passes a few numbers, and gets a mesh. Every
generator here is a closed-form (or near closed-form) parametric equation, so the
result is deterministic, resolution-controlled, and cheap.

    from forge3d.geometry.surfaces import build_shape
    mesh = build_shape("supershape", {"m": 6, "n1": 0.3, "n2": 0.6})

Shapes are split into two honest groups:

* **solids** — closed manifolds. Safe for printing and for game assets. These
  pass the `print` validation profile.
* **open** — parametric patches with a boundary (Möbius strip, helicoid). Visually
  interesting, not printable as-is. `validate_model` will say so, which is correct.

Each entry carries its equation as text, so `list_shapes` can show an agent what
the parameters actually mean instead of making it guess.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import trimesh

TAU = 2.0 * math.pi


# ---------------------------------------------------------------------------
# catalogue entry
# ---------------------------------------------------------------------------
@dataclass
class ShapeSpec:
    name: str
    equation: str
    params: dict = field(default_factory=dict)
    builder: Callable | None = None
    closed: bool = True          # closed manifold?
    family: str = "surface"      # surface | solid_of_revolution | swept | fractal | mechanical
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "equation": self.equation,
            "params": self.params,
            "closed": self.closed,
            "family": self.family,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# mesh helpers
# ---------------------------------------------------------------------------
def _rings_to_mesh(rings: np.ndarray, cap_start: str = "flat",
                   cap_end: str = "flat", loop: bool = False) -> trimesh.Trimesh:
    """Build a closed mesh from a stack of rings — the one primitive everything uses.

    `rings` is (n_rings, n_points, 3). Rings are stitched into a wall; the ends are
    closed with either a flat fan (`"flat"`), a single apex vertex (`"apex"`, for
    surfaces that converge to a pole), or left open (`None`).

    This exists because hand-writing the caps per shape produced a pile of
    "almost watertight" meshes: a pole ring that collapses to a point leaves
    degenerate faces, and `process=True` deletes them, leaving a hole. Capping
    with an explicit apex vertex instead is what makes the result actually closed.
    """
    rings = np.asarray(rings, dtype=float)
    n, nu = rings.shape[0], rings.shape[1]

    vertices = rings.reshape(-1, 3).copy()
    idx = np.arange(n * nu).reshape(n, nu).astype(int)
    extra: list[np.ndarray] = []
    base = n * nu

    def add_vertex(point: np.ndarray) -> int:
        nonlocal base
        extra.append(np.asarray(point, dtype=float).reshape(1, 3))
        v = base
        base += 1
        return v

    if cap_start == "apex":
        idx[0, :] = add_vertex(rings[0].mean(axis=0))
    if cap_end == "apex":
        idx[-1, :] = add_vertex(rings[-1].mean(axis=0))

    nxt = np.roll(np.arange(nu), -1)
    faces: list[np.ndarray] = []

    rows = list(range(n - 1)) + ([n - 1] if loop else [])
    for r in rows:
        r2 = (r + 1) % n
        a, b = idx[r], idx[r2]
        faces.append(np.column_stack([a, b, b[nxt]]))
        faces.append(np.column_stack([a, b[nxt], a[nxt]]))

    if cap_start == "flat":
        centre = add_vertex(rings[0].mean(axis=0))
        ring = idx[0]
        faces.append(np.column_stack([np.full(nu, centre), ring[nxt], ring]))
    if cap_end == "flat":
        centre = add_vertex(rings[-1].mean(axis=0))
        ring = idx[-1]
        faces.append(np.column_stack([np.full(nu, centre), ring, ring[nxt]]))

    all_vertices = np.vstack([vertices] + extra) if extra else vertices
    mesh = trimesh.Trimesh(vertices=all_vertices, faces=np.vstack(faces), process=False)
    # An apex row makes every second wall triangle degenerate — (apex, b, apex).
    # trimesh keeps them, and a zero-area face is enough to fail `is_watertight`
    # even though the surface is topologically closed (0 boundary edges).
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    with contextlib.suppress(Exception):   # networkx is a declared dependency
        trimesh.repair.fix_winding(mesh)
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def _spherical_surface(radius_fn: Callable, nu: int = 96, nv: int = 48) -> trimesh.Trimesh:
    """A star-shaped surface r(theta, phi), capped at both poles."""
    theta = np.linspace(0.0, TAU, nu, endpoint=False)
    phi = np.linspace(1e-3, math.pi - 1e-3, nv)
    tt, pp = np.meshgrid(theta, phi, indexing="ij")

    r = np.asarray(radius_fn(tt, pp), dtype=float)
    rings = np.stack([
        r * np.sin(pp) * np.cos(tt),
        r * np.sin(pp) * np.sin(tt),
        r * np.cos(pp),
    ], axis=-1)
    # (nu, nv, 3) -> (nv, nu, 3): rings run pole to pole
    return _rings_to_mesh(np.transpose(rings, (1, 0, 2)), "apex", "apex")


def _tube_from_curve(points: np.ndarray, radius: float, sides: int = 16,
                     loop: bool = False) -> trimesh.Trimesh:
    """Sweep a circle along a polyline using parallel-transport frames.

    Parallel transport matters: naive Frenet frames flip at inflection points and
    put a visible twist in the tube. This keeps the frame continuous.
    """
    pts = np.asarray(points, dtype=float)
    n = len(pts)

    tangents = np.gradient(pts, axis=0)
    tangents /= np.maximum(np.linalg.norm(tangents, axis=1, keepdims=True), 1e-12)

    seed = np.array([1.0, 0.0, 0.0])
    if abs(np.dot(seed, tangents[0])) > 0.9:
        seed = np.array([0.0, 1.0, 0.0])
    normal = np.cross(tangents[0], seed)
    normal /= np.linalg.norm(normal)

    normals = [normal]
    for i in range(1, n):
        prev_t, t_cur = tangents[i - 1], tangents[i]
        axis = np.cross(prev_t, t_cur)
        sin_a = np.linalg.norm(axis)
        if sin_a < 1e-9:
            normals.append(normals[-1])
            continue
        axis /= sin_a
        cos_a = float(np.clip(np.dot(prev_t, t_cur), -1.0, 1.0))
        angle = math.atan2(sin_a, cos_a)
        v = normals[-1]
        rotated = (v * math.cos(angle)
                   + np.cross(axis, v) * math.sin(angle)
                   + axis * np.dot(axis, v) * (1 - math.cos(angle)))
        normals.append(rotated / np.linalg.norm(rotated))
    normals = np.asarray(normals)
    binormals = np.cross(tangents, normals)

    angles = np.linspace(0, TAU, sides, endpoint=False)
    rings = np.empty((n, sides, 3))
    for i in range(n):
        rings[i] = (pts[i]
                    + radius * np.cos(angles)[:, None] * normals[i]
                    + radius * np.sin(angles)[:, None] * binormals[i])

    if loop:
        return _rings_to_mesh(rings, None, None, loop=True)
    return _rings_to_mesh(rings, "flat", "flat")


def _revolve(profile: np.ndarray, segments: int = 96) -> trimesh.Trimesh:
    """Solid of revolution: spin a 2D profile (x = radius, y = height) around Y."""
    prof = np.asarray(profile, dtype=float)
    angles = np.linspace(0, TAU, segments, endpoint=False)
    cos_a, sin_a = np.cos(angles), np.sin(angles)

    rings = np.stack([
        np.outer(prof[:, 0], cos_a),
        np.repeat(prof[:, 1][:, None], segments, axis=1),
        np.outer(prof[:, 0], sin_a),
    ], axis=-1)

    start = "apex" if prof[0, 0] < 1e-9 else "flat"
    end = "apex" if prof[-1, 0] < 1e-9 else "flat"
    return _rings_to_mesh(rings, start, end)


def _extrude_polygon(points_2d: np.ndarray, height: float,
                     twist: float = 0.0) -> trimesh.Trimesh:
    """Extrude a closed polygon, optionally twisting it along the axis."""
    pts = np.asarray(points_2d, dtype=float)
    cos_a, sin_a = math.cos(twist), math.sin(twist)

    bottom = np.column_stack([pts[:, 0], pts[:, 1], np.zeros(len(pts))])
    top = np.column_stack([
        pts[:, 0] * cos_a - pts[:, 1] * sin_a,
        pts[:, 0] * sin_a + pts[:, 1] * cos_a,
        np.full(len(pts), float(height)),
    ])
    return _rings_to_mesh(np.stack([bottom, top]), "flat", "flat")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def supershape(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Gielis superformula — one equation that covers flowers, stars and blobs."""
    m1 = float(params.get("m", 6))
    n1 = float(params.get("n1", 0.3))
    n2 = float(params.get("n2", 0.6))
    n3 = float(params.get("n3", 0.6))
    m2 = float(params.get("m2", m1))
    n1b = float(params.get("n1b", n1))
    n2b = float(params.get("n2b", n2))
    n3b = float(params.get("n3b", n3))
    scale = float(params.get("scale", 1.0))

    def radius(tt, pp):
        a = np.abs(np.cos(m1 * tt / 4.0) / 1.0) ** n2 + np.abs(np.sin(m1 * tt / 4.0)) ** n3
        r1 = a ** (-1.0 / n1)

        b = np.abs(np.cos(m2 * pp / 4.0)) ** n2b + np.abs(np.sin(m2 * pp / 4.0)) ** n3b
        r2 = b ** (-1.0 / n1b)
        return scale * r1 * r2

    return _spherical_surface(radius, nu, nv)


def spherical_harmonic(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """r = sum of real spherical harmonics — the classic 'mathematical blob'."""
    seed = int(params.get("seed", 3))
    scale = float(params.get("scale", 1.0))
    terms = int(params.get("terms", 3))
    rng = np.random.default_rng(seed)

    chosen = []
    for _ in range(terms):
        m = int(rng.integers(2, 7))
        amp = float(rng.uniform(0.08, 0.22))
        phase = float(rng.uniform(0, TAU))
        chosen.append((m, amp, phase))

    def radius(tt, pp):
        r = np.ones_like(tt)
        for m, amp, phase in chosen:
            r = r + amp * np.sin(m * tt + phase) * np.sin(pp) ** max(1, m // 2)
        return scale * np.maximum(r, 0.25)

    return _spherical_surface(radius, nu, nv)


def superellipsoid(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """|x/a|^e + |y/b|^e + |z/c|^e = 1 — cubes, pillows and lenses from two exponents."""
    e1 = float(params.get("e1", 0.5))
    e2 = float(params.get("e2", 0.5))
    a = float(params.get("a", 1.0))
    b = float(params.get("b", 1.0))
    c = float(params.get("c", 1.0))

    theta = np.linspace(0, TAU, nu, endpoint=False)
    phi = np.linspace(-math.pi / 2 + 1e-4, math.pi / 2 - 1e-4, nv)
    tt, pp = np.meshgrid(theta, phi, indexing="ij")

    def sgn_pow(v, e):
        return np.sign(v) * np.abs(v) ** e

    x = a * sgn_pow(np.cos(pp), e1) * sgn_pow(np.cos(tt), e2)
    y = b * sgn_pow(np.cos(pp), e1) * sgn_pow(np.sin(tt), e2)
    z = c * sgn_pow(np.sin(pp), e1)
    rings = np.stack([x, y, z], axis=-1)          # (nu, nv, 3) -> rings over theta
    return _rings_to_mesh(np.transpose(rings, (1, 0, 2)), "apex", "apex")


def torus_knot(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """(p, q) torus knot: x = (R + r cos qφ) cos pφ ..."""
    p = int(params.get("p", 2))
    q = int(params.get("q", 3))
    big_r = float(params.get("R", 2.0))
    tube = float(params.get("tube", 0.35))
    samples = int(params.get("samples", max(240, nu * 4)))

    t = np.linspace(0, TAU, samples)
    pts = np.column_stack([
        (big_r + tube * np.cos(q * t)) * np.cos(p * t),
        (big_r + tube * np.cos(q * t)) * np.sin(p * t),
        tube * np.sin(q * t),
    ])
    return _tube_from_curve(pts, tube, sides=max(8, nv // 3), loop=True)


def lissajous(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Lissajous knot: three sinusoids with different frequencies."""
    a = int(params.get("a", 3))
    b = int(params.get("b", 4))
    c = int(params.get("c", 5))
    delta = float(params.get("delta", math.pi / 2))
    radius = float(params.get("radius", 0.18))
    samples = int(params.get("samples", max(320, nu * 4)))

    t = np.linspace(0, TAU, samples)
    pts = np.column_stack([
        np.sin(a * t + delta),
        np.sin(b * t),
        np.sin(c * t + delta / 2),
    ]) * float(params.get("scale", 1.0))
    return _tube_from_curve(pts, radius, sides=max(8, nv // 3), loop=True)


def spring(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Helix swept into a tube: x = R cos t, y = R sin t, z = pitch·t."""
    turns = float(params.get("turns", 5))
    radius = float(params.get("radius", 1.0))
    pitch = float(params.get("pitch", 0.5))
    wire = float(params.get("wire", 0.15))
    samples = int(params.get("samples", max(200, nu * 3)))

    t = np.linspace(0, TAU * turns, samples)
    pts = np.column_stack([radius * np.cos(t), radius * np.sin(t), pitch * t])
    return _tube_from_curve(pts, wire, sides=max(8, nv // 3), loop=False)


def seashell(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Logarithmic spiral with a growing tube — a shell from two constants."""
    turns = float(params.get("turns", 3.2))
    growth = float(params.get("growth", 0.28))
    base_radius = float(params.get("base_radius", 0.12))
    samples = int(params.get("samples", max(260, nu * 3)))

    t = np.linspace(0, TAU * turns, samples)
    spiral = base_radius * np.exp(growth * t)
    pts = np.column_stack([
        spiral * np.cos(t),
        spiral * np.sin(t),
        spiral * 0.55,
    ])
    wire = max(0.02, float(params.get("wire", 0.06)) * float(params.get("scale", 1.0)))
    return _tube_from_curve(pts, wire, sides=max(8, nv // 3), loop=False)


def involute_gear(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Real involute gear teeth — the profile that actually meshes.

    Involute of a circle: x = r(cos t + t sin t), y = r(sin t - t cos t).
    Straight radial teeth look right and do not work; this is the shape a
    gearbox needs.
    """
    teeth = int(params.get("teeth", 20))
    module = float(params.get("module", 0.2))
    thickness = float(params.get("thickness", 0.4))
    pressure = math.radians(float(params.get("pressure_angle", 20)))

    pitch_r = module * teeth / 2.0
    addendum = module
    dedendum = 1.25 * module
    dedendum_r = pitch_r - dedendum
    base_r = pitch_r * math.cos(pressure)

    def involute(t):
        return np.column_stack([
            base_r * (np.cos(t) + t * np.sin(t)),
            base_r * (np.sin(t) - t * np.cos(t)),
        ])

    t_max = math.sqrt((pitch_r + addendum) ** 2 / base_r ** 2 - 1)
    samples = max(6, nu // 8)
    flank = involute(np.linspace(0, t_max, samples))
    flank_angle = np.arctan2(flank[-1, 1], flank[-1, 0])

    pitch_angle = TAU / teeth
    half_tooth = pitch_angle / 4.0

    one_tooth = []
    for point in flank:
        ang = np.arctan2(point[1], point[0])
        r = np.linalg.norm(point)
        one_tooth.append([r * math.cos(ang - flank_angle + half_tooth),
                          r * math.sin(ang - flank_angle + half_tooth)])
    one_tooth = np.asarray(one_tooth)
    mirrored = one_tooth[::-1].copy()
    mirrored[:, 0] = mirrored[:, 0]
    mirrored[:, 1] = -mirrored[:, 1]

    profile = []
    for i in range(teeth):
        offset = i * pitch_angle
        rot = np.array([[math.cos(offset), -math.sin(offset)],
                        [math.sin(offset), math.cos(offset)]])
        for point in one_tooth:
            profile.append(rot @ point)
        for point in mirrored[::-1]:
            profile.append(rot @ point)
        # Root between teeth: without this the gap is a chord at the base circle
        # and the gear reads as a star. The dedendum circle is what makes the
        # valleys deep enough for a mating tooth to actually enter.
        gap_angle = offset + pitch_angle / 2.0
        profile.append(np.array([dedendum_r * math.cos(gap_angle),
                                 dedendum_r * math.sin(gap_angle)]))

    return _extrude_polygon(np.asarray(profile), thickness)


def twisted_prism(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """A regular polygon extruded with a twist — columns, drill bits, towers."""
    sides = int(params.get("sides", 6))
    radius = float(params.get("radius", 1.0))
    height = float(params.get("height", 2.0))
    twist = float(params.get("twist", math.pi / 3))

    angles = np.linspace(0, TAU, sides, endpoint=False)
    pts = np.column_stack([radius * np.cos(angles), radius * np.sin(angles)])
    return _extrude_polygon(pts, height, twist=twist)


def lathe(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Solid of revolution from a profile: [(radius, height), ...]."""
    profile = params.get("profile")
    if not profile:
        # default: a vase silhouette
        profile = [[0.0, 0.0], [0.6, 0.0], [0.75, 0.5], [0.45, 1.4],
                   [0.7, 2.0], [0.5, 2.4], [0.0, 2.4]]
    return _revolve(np.asarray(profile, dtype=float), segments=max(24, nu))


def mobius(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Möbius strip — one side, one edge. Open surface: not printable as a solid."""
    radius = float(params.get("radius", 1.0))
    width = float(params.get("width", 0.4))
    twists = float(params.get("twists", 1))

    u = np.linspace(0, TAU, nu)
    v = np.linspace(-width, width, nv)
    uu, vv = np.meshgrid(u, v, indexing="ij")

    x = (radius + vv * np.cos(twists * uu / 2)) * np.cos(uu)
    y = (radius + vv * np.cos(twists * uu / 2)) * np.sin(uu)
    z = vv * np.sin(twists * uu / 2)
    rings = np.stack([x, y, z], axis=-1)
    return _rings_to_mesh(np.transpose(rings, (1, 0, 2)), None, None)


def helicoid(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Helicoid: x = v cos u, y = v sin u, z = c·u — a screw surface."""
    radius = float(params.get("radius", 1.0))
    pitch = float(params.get("pitch", 0.4))
    turns = float(params.get("turns", 1.0))

    u = np.linspace(0, TAU * turns, nu)
    v = np.linspace(-radius, radius, nv)
    uu, vv = np.meshgrid(u, v, indexing="ij")
    x = vv * np.cos(uu)
    y = vv * np.sin(uu)
    z = pitch * uu
    rings = np.stack([x, y, z], axis=-1)
    return _rings_to_mesh(np.transpose(rings, (1, 0, 2)), None, None)


def catenoid(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Catenoid: x = c·cosh(v/c)·cos u — the only minimal surface of revolution."""
    c = float(params.get("c", 0.6))
    height = float(params.get("height", 1.0))

    u = np.linspace(0, TAU, nu, endpoint=False)
    v = np.linspace(-height, height, nv)
    uu, vv = np.meshgrid(u, v, indexing="ij")
    r = c * np.cosh(vv / c)
    x = r * np.cos(uu)
    y = r * np.sin(uu)
    z = vv
    rings = np.stack([x, y, z], axis=-1)
    return _rings_to_mesh(np.transpose(rings, (1, 0, 2)), None, None)


def menger_sponge(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Menger sponge, one recursion level = 20 cubes.

    Returned as a multi-body mesh: 20^n separate closed boxes. It is correct
    geometry, not a single manifold — `validate_model` reports the body count,
    which is the honest answer for a fractal.
    """
    level = int(params.get("level", 2))
    size = float(params.get("size", 3.0))

    cells = [(0, 0, 0)]
    step = size
    for _ in range(level):
        step /= 3.0
        new = []
        for cx, cy, cz in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        removed = sum(1 for v in (dx, dy, dz) if v == 0)
                        if removed >= 2:
                            continue
                        new.append((cx + dx * step, cy + dy * step, cz + dz * step))
        cells = new

    boxes = []
    for cx, cy, cz in cells:
        box = trimesh.creation.box(extents=[step, step, step])
        box.apply_translation([cx, cy, cz])
        boxes.append(box)
    mesh = trimesh.util.concatenate(boxes)
    mesh.merge_vertices()
    return mesh


def bumpy_sphere(params: dict, nu: int, nv: int) -> trimesh.Trimesh:
    """Sphere displaced by a product of sinusoids — the simplest useful blob."""
    radius = float(params.get("radius", 1.0))
    amplitude = float(params.get("amplitude", 0.15))
    freq_u = float(params.get("freq_u", 6))
    freq_v = float(params.get("freq_v", 4))

    def r(tt, pp):
        return radius * (1.0 + amplitude * np.sin(freq_u * tt) * np.sin(freq_v * pp))

    return _spherical_surface(r, nu, nv)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
SHAPES: dict[str, ShapeSpec] = {
    "supershape": ShapeSpec(
        "supershape",
        "r(θ) = (|cos(mθ/4)|^n2 + |sin(mθ/4)|^n3)^(-1/n1),  r(θ,φ) = r(θ)·r(φ)",
        {"m": 6, "n1": 0.3, "n2": 0.6, "n3": 0.6, "scale": 1.0},
        supershape, closed=True, family="surface",
        notes="One equation for flowers, stars, gears and organic blobs. Start with m=6,n1=0.3.",
    ),
    "spherical_harmonic": ShapeSpec(
        "spherical_harmonic",
        "r(θ,φ) = 1 + Σ aᵢ·sin(mᵢθ + φᵢ)·sin(φ)^(mᵢ/2)",
        {"seed": 3, "terms": 3, "scale": 1.0},
        spherical_harmonic, closed=True, family="surface",
        notes="Seeded lumpy blobs. Change `seed` for a different one.",
    ),
    "superellipsoid": ShapeSpec(
        "superellipsoid",
        "|x/a|^(2/e1) + |y/b|^(2/e1) + |z/c|^(2/e1) = 1  (e1,e2 shape it)",
        {"e1": 0.5, "e2": 0.5, "a": 1.0, "b": 1.0, "c": 1.0},
        superellipsoid, closed=True, family="surface",
        notes="e=0.2 → boxy, e=1.0 → ellipsoid, e=2 → lens.",
    ),
    "torus_knot": ShapeSpec(
        "torus_knot",
        "x=(R+r·cos qφ)cos pφ, y=(R+r·cos qφ)sin pφ, z=r·sin qφ",
        {"p": 2, "q": 3, "R": 2.0, "tube": 0.35},
        torus_knot, closed=True, family="swept",
        notes="p,q coprime gives a true knot. (2,3) trefoil, (3,4), (5,2).",
    ),
    "lissajous": ShapeSpec(
        "lissajous",
        "x=sin(at+δ), y=sin(bt), z=sin(ct+δ/2)",
        {"a": 3, "b": 4, "c": 5, "delta": 1.5708, "radius": 0.18},
        lissajous, closed=True, family="swept",
        notes="Frequencies decide the knot. Coprime triples are the interesting ones.",
    ),
    "spring": ShapeSpec(
        "spring",
        "x=R·cos t, y=R·sin t, z=pitch·t",
        {"turns": 5, "radius": 1.0, "pitch": 0.5, "wire": 0.15},
        spring, closed=True, family="swept",
        notes="Real helical spring; printable, watertight.",
    ),
    "seashell": ShapeSpec(
        "seashell",
        "ρ(t) = ρ₀·e^(b·t), swept as a tube",
        {"turns": 3.2, "growth": 0.28, "base_radius": 0.12, "wire": 0.06},
        seashell, closed=True, family="swept",
        notes="Logarithmic spiral with growing radius — the shape shells actually have.",
    ),
    "involute_gear": ShapeSpec(
        "involute_gear",
        "involute: x=r(cos t + t·sin t), y=r(sin t − t·cos t)",
        {"teeth": 20, "module": 0.2, "thickness": 0.4, "pressure_angle": 20},
        involute_gear, closed=True, family="mechanical",
        notes="Real meshing tooth profile. pitch diameter = module × teeth.",
    ),
    "twisted_prism": ShapeSpec(
        "twisted_prism",
        "regular n-gon extruded with a twist about z",
        {"sides": 6, "radius": 1.0, "height": 2.0, "twist": 1.0472},
        twisted_prism, closed=True, family="solid_of_revolution",
        notes="Columns, towers, drill flutes.",
    ),
    "lathe": ShapeSpec(
        "lathe",
        "surface of revolution of a 2D profile [(radius, height), ...]",
        {"profile": None},
        lathe, closed=True, family="solid_of_revolution",
        notes="Give it a profile to make vases, bolts, bowls, chess pieces.",
    ),
    "bumpy_sphere": ShapeSpec(
        "bumpy_sphere",
        "r(θ,φ) = R·(1 + A·sin(f_u θ)·sin(f_v φ))",
        {"radius": 1.0, "amplitude": 0.15, "freq_u": 6, "freq_v": 4},
        bumpy_sphere, closed=True, family="surface",
        notes="Simplest useful deformation; good for testing a pipeline.",
    ),
    "mobius": ShapeSpec(
        "mobius",
        "x=(R+v·cos(u/2))cos u, y=(R+v·cos(u/2))sin u, z=v·sin(u/2)",
        {"radius": 1.0, "width": 0.4, "twists": 1},
        mobius, closed=False, family="surface",
        notes="One side, one edge. OPEN surface — not a printable solid.",
    ),
    "helicoid": ShapeSpec(
        "helicoid",
        "x=v·cos u, y=v·sin u, z=c·u",
        {"radius": 1.0, "pitch": 0.4, "turns": 1.0},
        helicoid, closed=False, family="surface",
        notes="Minimal surface, screw form. OPEN.",
    ),
    "catenoid": ShapeSpec(
        "catenoid",
        "x=c·cosh(v/c)·cos u, y=c·cosh(v/c)·sin u, z=v",
        {"c": 0.6, "height": 1.0},
        catenoid, closed=False, family="surface",
        notes="The only minimal surface of revolution. OPEN at both ends.",
    ),
    "menger_sponge": ShapeSpec(
        "menger_sponge",
        "recursive removal of the centre cross, 20 cubes per level",
        {"level": 2, "size": 3.0},
        menger_sponge, closed=False, family="fractal",
        notes="Multi-body: 20^level separate closed boxes, not one manifold.",
    ),
}


def list_shapes() -> list[dict]:
    return [spec.to_dict() for spec in SHAPES.values()]


def build_shape(name: str, params: dict | None = None, resolution: int = 96) -> trimesh.Trimesh:
    """Build one shape by name. `resolution` controls both grid directions."""
    key = (name or "").strip().lower()
    if key not in SHAPES:
        raise KeyError(f"unknown shape {name!r}; choose from {sorted(SHAPES)}")
    spec = SHAPES[key]
    merged = dict(spec.params)
    merged.update(params or {})
    nu = max(12, min(512, int(resolution)))
    nv = max(8, min(256, int(resolution) // 2))
    mesh = spec.builder(merged, nu, nv)
    mesh.metadata["shape"] = key
    mesh.metadata["params"] = {k: v for k, v in merged.items() if v is not None}
    return mesh
