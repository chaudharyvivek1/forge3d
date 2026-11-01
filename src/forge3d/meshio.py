"""Loading, converting and exporting meshes.

One rule: everything downstream (validate, render, viewer) talks in
`trimesh.Trimesh` or `trimesh.Scene`, never in raw bytes and never in a
format-specific object.
"""

from __future__ import annotations

from pathlib import Path

import trimesh

READABLE = {".stl", ".obj", ".ply", ".off", ".glb", ".gltf", ".dae", ".3mf", ".usd", ".usdz", ".fbx"}
WRITABLE = {".stl", ".obj", ".ply", ".off", ".glb", ".gltf", ".dae"}

# What each delivery target should receive.
RECOMMENDED = {
    "print": ".stl",
    "web": ".glb",
    "game": ".glb",
    "cad": ".glb",  # STEP comes from the CadQuery backend, not from a mesh
    "any": ".glb",
}


class MeshError(RuntimeError):
    pass


def load(path: str | Path) -> trimesh.Trimesh | trimesh.Scene:
    p = Path(path)
    if not p.exists():
        raise MeshError(f"no such file: {p}")
    if p.suffix.lower() not in READABLE:
        raise MeshError(f"unsupported input format {p.suffix!r}; try one of {sorted(READABLE)}")
    try:
        loaded = trimesh.load(str(p), force=None)
    except Exception as exc:  # trimesh raises a wide variety of errors
        raise MeshError(f"could not read {p.name}: {exc}") from exc
    if loaded is None:
        raise MeshError(f"{p.name} contained no geometry")
    return loaded


def as_mesh(obj: trimesh.Trimesh | trimesh.Scene) -> trimesh.Trimesh:
    """Flatten a scene into one mesh, keeping material info where possible."""
    if isinstance(obj, trimesh.Trimesh):
        return obj
    if isinstance(obj, trimesh.Scene):
        if not obj.geometry:
            raise MeshError("scene has no geometry")
        return _flatten(obj)
    raise MeshError(f"unexpected geometry type: {type(obj)!r}")


def _flatten(scene: trimesh.Scene) -> trimesh.Trimesh:
    """trimesh renamed dump(concatenate=True) to to_geometry() in 5.0 —
    support both so the package works on 4.x and 5.x."""
    if hasattr(scene, "to_geometry"):
        return scene.to_geometry()
    return scene.dump(concatenate=True)


def export(obj: trimesh.Trimesh | trimesh.Scene, out_path: str | Path, fmt: str | None = None) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{(fmt or p.suffix).lstrip('.').lower()}"
    if suffix not in WRITABLE:
        raise MeshError(f"cannot write {suffix!r}; supported: {sorted(WRITABLE)}")
    if p.suffix.lower() != suffix:
        p = p.with_suffix(suffix)
    data = obj.export(file_type=suffix.lstrip("."))
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return p


def convert(src: str | Path, dst: str | Path) -> Path:
    obj = load(src)
    return export(obj, dst)


def stats(obj: trimesh.Trimesh | trimesh.Scene) -> dict:
    """Format-agnostic geometry facts. Safe on broken meshes."""
    if isinstance(obj, trimesh.Scene):
        mesh = as_mesh(obj)
        scene_info = {"geometry_count": len(obj.geometry), "node_count": len(obj.graph.nodes)}
    else:
        mesh = obj
        scene_info = {"geometry_count": 1, "node_count": 1}

    extents = [float(x) for x in mesh.extents] if mesh.vertices.size else [0.0, 0.0, 0.0]
    bounds = mesh.bounds if mesh.vertices.size else None

    # body_count and volume both walk the face graph; without scipy installed
    # trimesh raises on access. Degrade to "unknown" instead of failing the
    # whole inspection — an agent can still act on counts and dimensions.
    try:
        body_count = int(mesh.body_count)
    except Exception:
        body_count = -1
    try:
        volume = float(mesh.volume) if mesh.is_volume else 0.0
    except Exception:
        volume = 0.0

    return {
        "vertices": len(mesh.vertices),
        "faces": len(mesh.faces),
        "triangles": len(mesh.faces),
        "extents": extents,
        "bounds": bounds.tolist() if bounds is not None else None,
        "center": mesh.centroid.tolist() if len(mesh.vertices) else [0.0, 0.0, 0.0],
        "is_watertight": bool(mesh.is_watertight),
        "is_winding_consistent": bool(mesh.is_winding_consistent),
        "volume": volume,
        "area": float(mesh.area) if mesh.area else 0.0,
        "euler_number": int(mesh.euler_number),
        "body_count": body_count,
        "has_uv": bool(getattr(getattr(mesh, "visual", None), "uv", None) is not None),
        **scene_info,
    }
