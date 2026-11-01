"""Is this mesh fit for its purpose?

Visual plausibility is not acceptance. A model that looks fine in a viewport can
be non-manifold, inside-out, 400x too large, or 3 million triangles. Each
delivery target has different hard requirements, so validation is profile-based
and returns machine-readable checks an agent can act on — every failure carries
a `hint` naming the fix.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import trimesh

from . import meshio
from .config import get_settings
from .spec import ModelSpec

PROFILES = ("print", "web", "game", "cad", "any")

ERROR, WARN, INFO = "error", "warn", "info"


@dataclass
class Check:
    id: str
    ok: bool
    severity: str = ERROR
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    ok: bool
    profile: str
    stats: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "profile": self.profile,
            "stats": self.stats,
            "failed": [asdict(c) for c in self.checks if not c.ok],
            "passed": [c.id for c in self.checks if c.ok],
            "summary": self.summary(),
        }

    def summary(self) -> str:
        bad = [c for c in self.checks if not c.ok]
        errs = [c for c in bad if c.severity == ERROR]
        warns = [c for c in bad if c.severity == WARN]
        if not errs:
            if not warns:
                return f"PASS — {self.profile} profile, {len(self.checks)} checks"
            return (
                f"PASS with {len(warns)} warning(s) — {self.profile} profile "
                f"({', '.join(c.id for c in warns)})"
            )
        return f"FAIL — {len(errs)} error(s), {len(warns)} warning(s) for the {self.profile} profile"


def _face_budget(profile: str, spec: ModelSpec | None) -> int | None:
    if spec and spec.poly_budget:
        return int(spec.poly_budget)
    s = get_settings()
    return {
        "web": s.max_faces_web,
        "game": s.max_faces_game,
        "print": s.max_faces_print,
    }.get(profile)


def validate(
    obj: trimesh.Trimesh | trimesh.Scene,
    profile: str = "any",
    spec: ModelSpec | None = None,
) -> Report:
    profile = (profile or "any").lower()
    if profile not in PROFILES:
        profile = "any"
    if profile == "any" and spec is not None:
        profile = spec.purpose

    st = meshio.stats(obj)
    mesh = meshio.as_mesh(obj)
    checks: list[Check] = []

    def add(cid: str, ok: bool, severity: str, detail: str = "", hint: str = "") -> None:
        checks.append(Check(cid, bool(ok), severity, detail, hint))

    # --- always required -------------------------------------------------
    add(
        "geometry_present",
        st["faces"] > 0,
        ERROR,
        f"{st['faces']} triangles",
        "build() returned nothing usable — check the source before regenerating.",
    )
    add(
        "dimensions_nonzero",
        max(st["extents"]) > 1e-6,
        ERROR,
        f"extents {[round(e, 4) for e in st['extents']]}",
        "the model collapsed to a plane or a point; check your units and boolean order.",
    )

    degenerate = int((mesh.area_faces < 1e-12).sum()) if st["faces"] else 0
    add(
        "no_degenerate_faces",
        degenerate == 0,
        ERROR if profile in ("print", "cad") else WARN,
        f"{degenerate} zero-area triangles",
        "call repair_model — it drops degenerate faces and merges duplicate vertices.",
    )

    budget = _face_budget(profile, spec)
    if budget:
        add(
            "face_budget",
            st["faces"] <= budget,
            ERROR if profile in ("web", "game") else WARN,
            f"{st['faces']} / {budget} triangles",
            "call repair_model with decimate=true, or rebuild with a coarser mesh.",
        )

    # --- profile specific ------------------------------------------------
    if profile in ("print", "cad"):
        add(
            "watertight",
            st["is_watertight"],
            ERROR,
            "surface is closed" if st["is_watertight"] else "open edges found",
            "a slicer cannot infer solid from an open surface — repair_model with fill_holes=true.",
        )
        add(
            "winding_consistent",
            st["is_winding_consistent"],
            ERROR,
            "normals point outward consistently" if st["is_winding_consistent"] else "inconsistent normals",
            "inside-out faces print as holes — repair_model fixes winding.",
        )
        add(
            "positive_volume",
            st["volume"] > 0,
            ERROR,
            f"volume {st['volume']:.3f} (model units³)",
            "zero or negative volume usually means flipped normals or a non-closed shell.",
        )
        add(
            "single_body",
            st["body_count"] <= 1,
            WARN,
            f"{st['body_count']} disconnected bodies",
            "loose shells print as separate parts — boolean-union them or accept multi-part output.",
        )

    if profile == "web":
        has_uv = bool(st.get("has_uv"))
        add(
            "uv_mapping",
            has_uv,
            WARN,
            "UVs present" if has_uv else "no UV coordinates",
            "without UVs you cannot texture in the browser; a hosted text-to-3D backend usually provides them.",
        )
        add(
            "single_body",
            st["body_count"] <= 8,
            WARN,
            f"{st['body_count']} bodies",
            "many bodies means many draw calls; merge before shipping to mobile web.",
        )

    if profile == "game":
        add(
            "lod_headroom",
            st["faces"] <= (budget or 60_000),
            WARN,
            f"{st['faces']} triangles before decimation",
            "ship a decimated LOD chain: 100% / 50% / 20% of the source triangle count.",
        )
        add(
            "single_body",
            st["body_count"] <= 4,
            WARN,
            f"{st['body_count']} bodies",
            "each body is a draw call and a separate skinning target.",
        )

    # --- spec conformance ------------------------------------------------
    if spec is not None and spec.extent:
        target = spec.extent
        actual = st["extents"]
        # Compare the largest axis only: scaling is uniform, so a uniform factor
        # that nails the dominant dimension legitimately moves the others.
        t_max, a_max = max(target), max(actual)
        drift = abs(a_max / t_max - 1.0) if t_max else 0.0
        add(
            "dimensions_match_spec",
            drift <= 0.05,
            WARN,
            f"largest axis {a_max:.1f} vs {t_max:g} {spec.units} ({drift * 100:.1f}% off)",
            "scale in build() for exact per-axis control, or pass scale_to_spec for uniform scaling.",
        )
        if max(actual) > 0:
            add(
                "units_plausible",
                0.1 <= max(actual) <= 10_000,
                WARN,
                f"largest extent {max(actual):.2f} {spec.units}",
                "a 0.001mm or 500m part is usually a unit mix-up (m vs mm).",
            )

    ok = not any((not c.ok) and c.severity == ERROR for c in checks)
    return Report(ok=ok, profile=profile, stats=st, checks=checks)


def scale_to_spec(obj: trimesh.Trimesh | trimesh.Scene, spec: ModelSpec):
    """Uniformly scale so the largest axis matches the brief."""
    if not spec.extent:
        return obj
    mesh = meshio.as_mesh(obj)
    target = max(spec.extent)
    current = max(mesh.extents)
    if current <= 0:
        return obj
    factor = target / current
    if abs(factor - 1.0) < 1e-9:
        return obj
    mesh.apply_scale(factor)
    return mesh


def check_face_areas(obj) -> np.ndarray:
    mesh = meshio.as_mesh(obj)
    return mesh.area_faces
