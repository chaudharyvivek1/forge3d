"""Make a broken mesh usable.

Everything here is conservative and reversible in intent: merge, drop, fill,
re-orient. Nothing invents detail. Every action taken is reported so the agent
can tell a human what changed and why.
"""

from __future__ import annotations

from pathlib import Path

import trimesh

from . import meshio


def repair(
    obj: trimesh.Trimesh | trimesh.Scene,
    merge: bool = True,
    drop_degenerate: bool = True,
    fix_normals: bool = True,
    fill_holes: bool = True,
    decimate_to: int | None = None,
):
    """Return (mesh, actions). `actions` is a plain list of human-readable strings."""
    mesh = meshio.as_mesh(obj)
    actions: list[str] = []

    before = {"faces": len(mesh.faces), "vertices": len(mesh.vertices)}

    if drop_degenerate and len(mesh.faces):
        keep = mesh.nondegenerate_faces()
        if not keep.all():
            mesh.update_faces(keep)
            actions.append(f"dropped {int((~keep).sum())} degenerate faces")

        unique = mesh.unique_faces()
        if not unique.all():
            mesh.update_faces(unique)
            actions.append(f"dropped {int((~unique).sum())} duplicate faces")

    if merge:
        mesh.merge_vertices()
        mesh.remove_unreferenced_vertices()
        actions.append("merged duplicate vertices")

    if fix_normals:
        was_consistent = mesh.is_winding_consistent
        trimesh.repair.fix_normals(mesh)
        if not was_consistent:
            actions.append("re-oriented inconsistent normals")

    if fill_holes and not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
        actions.append("filled holes" if mesh.is_watertight else "attempted hole fill (still open)")

    if decimate_to and len(mesh.faces) > decimate_to:
        reduced = _decimate(mesh, decimate_to)
        if reduced is not None:
            mesh = reduced
            actions.append(f"decimated {before['faces']} -> {len(mesh.faces)} triangles")
        else:
            actions.append(
                "decimation skipped: install `fast-simplification` (pip install fast-simplification)"
            )

    if not actions:
        actions.append("no repairs needed")

    return mesh, actions


def _decimate(mesh: trimesh.Trimesh, target: int):
    try:
        return mesh.simplify_quadric_decimation(face_count=target)
    except Exception:
        try:
            import fast_simplification  # noqa: F401

            return mesh.simplify_quadric_decimation(face_count=target)
        except Exception:
            return None


def repair_file(src: str | Path, dst: str | Path | None = None, **kwargs):
    obj = meshio.load(src)
    mesh, actions = repair(obj, **kwargs)
    out = meshio.export(mesh, dst or Path(src).with_name(Path(src).stem + "_repaired.glb"))
    return out, actions, mesh
