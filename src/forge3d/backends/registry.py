"""Backend discovery and `auto` routing.

The routing table is the answer to "which engine should I use?" — the single
question agents get wrong most often. It is data, not a prompt, so it is
testable.
"""

from __future__ import annotations

from ..spec import ModelSpec
from .base import Backend, BackendResult
from .blender import BlenderBackend
from .cadquery_backend import CadQueryBackend
from .hosted import HostedBackend
from .openscad import OpenscadBackend
from .procedural import ProceduralBackend

ALL: dict[str, Backend] = {
    b.name: b
    for b in (
        ProceduralBackend(),
        OpenscadBackend(),
        BlenderBackend(),
        CadQueryBackend(),
        HostedBackend(),
    )
}


def get_backend(name: str | None) -> Backend:
    key = (name or "procedural").lower()
    if key not in ALL:
        raise KeyError(f"unknown backend {name!r}; choose from {sorted(ALL)}")
    return ALL[key]


def list_backends() -> list[dict]:
    return [b.describe() for b in ALL.values()]


def resolve_auto(spec: ModelSpec, source: str | None = None, purpose_hint: str | None = None) -> str:
    """Pick a backend from the brief. Explicit signals beat heuristics."""
    purpose = (purpose_hint or spec.purpose or "any").lower()

    if source:
        head = source[:400].lower()
        if "bpy" in head or "bmesh" in head:
            return "blender"
        if "cadquery" in head or "cq." in head:
            return "cadquery"
        if "module" in head and ("cube(" in head or "cylinder(" in head or "difference()" in head):
            return "openscad"

    if purpose == "cad" and ALL["cadquery"].available():
        return "cadquery"
    if purpose == "print":
        # Printing needs a solid: OpenSCAD if present, else agent-written trimesh.
        return "openscad" if ALL["openscad"].available() else "procedural"
    if purpose == "game" and ALL["blender"].available():
        return "blender"
    if purpose == "web":
        # Web wants a textured GLB; a hosted model is the only textured route.
        return "hosted" if ALL["hosted"].available() else "procedural"
    if spec.references and ALL["hosted"].available():
        return "hosted"
    return "procedural"


def run(
    backend_name: str,
    spec: ModelSpec,
    out_dir,
    source: str | None = None,
    **kwargs,
) -> tuple[str, BackendResult]:
    name = resolve_auto(spec, source) if (backend_name or "auto").lower() == "auto" else backend_name.lower()
    backend = get_backend(name)
    return name, backend.generate(spec, out_dir, source=source, **kwargs)
