"""Turn a mesh into images the agent can actually look at.

This is the feedback loop that separates an agent that iterates from one that
guesses. A generation step with no render is a blind write: the agent cannot
tell a correct bracket from a floating pile of cylinders.

Rendering is deliberately CPU-only and dependency-light (matplotlib + a
painter's-algorithm poly collection) so it works in a headless sandbox with no
GPU and no OpenGL. It is a *review* render, not a beauty shot — for beauty, use
the Three.js viewer.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import meshio

VIEWS = {
    "iso": (28, 45),
    "front": (0, -90),
    "right": (0, 0),
    "top": (89, -90),
}

MAX_PREVIEW_FACES = 20_000


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except Exception:
        return False
    return True


def _shade(mesh, base=(0.62, 0.66, 0.72)):
    """Lambert shading baked into per-face colours."""
    normals = mesh.face_normals
    light = np.array([0.4, 0.55, 0.75])
    light = light / np.linalg.norm(light)
    lam = np.clip(normals @ light, 0.0, 1.0)
    ambient = 0.38
    intensity = ambient + (1.0 - ambient) * lam
    rgb = np.clip(np.array(base)[None, :] * intensity[:, None], 0, 1)
    return np.hstack([rgb, np.ones((len(rgb), 1))])


def render_views(
    obj,
    out_dir: str | Path,
    name: str = "model",
    size: int = 720,
    views: tuple[str, ...] = ("iso", "front", "right", "top"),
) -> dict:
    """Write one PNG per view. Returns {view: path} plus any warnings."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not available():
        return {"ok": False, "error": "matplotlib not installed (pip install matplotlib)", "previews": {}}

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    mesh = meshio.as_mesh(obj)
    if len(mesh.faces) == 0:
        return {"ok": False, "error": "no faces to render", "previews": {}}

    step = max(1, len(mesh.faces) // MAX_PREVIEW_FACES)
    tris = mesh.triangles[::step]
    colors = _shade(mesh)[::step]

    lo, hi = mesh.bounds
    center = (lo + hi) / 2.0
    span = float(max(hi - lo)) or 1.0
    half = span * 0.58

    previews: dict[str, str] = {}
    for view in views:
        if view not in VIEWS:
            continue
        elev, azim = VIEWS[view]
        fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
        ax = fig.add_subplot(111, projection="3d")

        coll = Poly3DCollection(
            tris,
            facecolors=colors,
            edgecolors=(0.1, 0.11, 0.13, 0.25),
            linewidths=0.15,
            zsort="average",
        )
        ax.add_collection3d(coll)

        ax.set_xlim(center[0] - half, center[0] + half)
        ax.set_ylim(center[1] - half, center[1] + half)
        ax.set_zlim(center[2] - half, center[2] + half)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        ax.set_facecolor("white")
        fig.patch.set_facecolor("white")
        ax.set_title(
            f"{name} — {view}  |  {len(mesh.faces):,} tris  |  "
            f"{span:.1f} units",
            fontsize=9,
            color="#333333",
        )

        path = out_dir / f"{name}_{view}.png"
        fig.tight_layout(pad=0.2)
        fig.savefig(path, facecolor="white")
        plt.close(fig)
        previews[view] = str(path)

    return {"ok": bool(previews), "previews": previews, "decimated_for_render": step > 1}


def render_file(src: str | Path, out_dir: str | Path, name: str | None = None, **kwargs) -> dict:
    obj = meshio.load(src)
    return render_views(obj, out_dir, name=name or Path(src).stem, **kwargs)
