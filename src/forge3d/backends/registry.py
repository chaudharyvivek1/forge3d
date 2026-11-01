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
from .image_backend import ImageBackend
from .openscad import OpenscadBackend
from .procedural import ProceduralBackend

ALL: dict[str, Backend] = {
    b.name: b
    for b in (
        ProceduralBackend(),
        ImageBackend(),
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


# Tokens that only appear in OpenSCAD source. Requiring two of them avoids
# mis-routing Python that merely calls something named `cube`.
_SCAD_TOKENS = (
    "module ", "difference(", "union(", "intersection(", "translate(",
    "rotate(", "linear_extrude", "rotate_extrude", "hull(", "minkowski(",
    "$fn", "cylinder(", "cube(", "sphere(", "polyhedron(",
)


def _looks_like_scad(src: str) -> bool:
    head = src[:2000].lower()
    if ("def " in head or "import " in head) and "module " not in head:
        return False
    return sum(token in head for token in _SCAD_TOKENS) >= 2


def resolve_auto(spec: ModelSpec, source: str | None = None, purpose_hint: str | None = None) -> str:
    """Pick a backend from the brief. Explicit signals beat heuristics.

    `spec.mode` is a hard constraint, not a preference: `local` never returns a
    paid provider, and `cloud` never returns a local engine. Silently upgrading a
    user into a billable API call is the kind of helpfulness nobody wants.
    """
    purpose = (purpose_hint or spec.purpose or "any").lower()
    mode = (getattr(spec, "mode", "auto") or "auto").lower()
    hosted_ok = mode in ("auto", "cloud") and ALL["hosted"].available()

    if mode == "cloud":
        return "hosted"

    if source:
        head = source[:2000].lower()
        if "bpy" in head or "bmesh" in head:
            return "blender"
        if "cadquery" in head or "cq." in head:
            return "cadquery"
        if _looks_like_scad(source):
            return "openscad"

    if purpose == "cad" and ALL["cadquery"].available():
        return "cadquery"
    if purpose == "print":
        # Printing needs a solid: OpenSCAD if present, else agent-written trimesh.
        return "openscad" if ALL["openscad"].available() else "procedural"
    if purpose == "game" and ALL["blender"].available():
        return "blender"
    if purpose == "web":
        # Web wants a textured GLB, and a hosted model is the only textured
        # route — but only when the caller has not asked to stay local.
        return "hosted" if hosted_ok else "procedural"
    # A local image file is the one case where image-to-3D is deterministic and
    # needs no GPU: derive real geometry from the pixels instead of shipping the
    # file to a provider.
    if spec.references:
        first = str(spec.references[0])
        if not first.startswith(("http://", "https://")):
            from pathlib import Path

            if Path(first).expanduser().is_file():
                return "image"
        if hosted_ok:
            return "hosted"
    return "procedural"


def mode_violation(mode: str, backend_name: str) -> str | None:
    """Is this backend allowed under this mode? Returns a reason, or None.

    `resolve_auto` honours `spec.mode`, but a caller can name a backend
    explicitly and walk straight past it. That made the local/cloud guarantee a
    promise the code did not keep: `backend="hosted", mode="local"` would still
    make a billable call. The constraint is checked here, at the single point
    every generation goes through.
    """
    mode = (mode or "auto").lower()
    name = (backend_name or "").lower()
    if mode == "local" and name == "hosted":
        return ("mode='local' forbids the hosted backend, which bills a provider. "
                "Use a local engine, or pass mode='auto'/'cloud' if spending is intended.")
    if mode == "cloud" and name and name != "hosted":
        return ("mode='cloud' means hosted providers only; "
                f"{name!r} runs locally. Pass mode='auto'/'local', or backend='hosted'.")
    return None


def run(
    backend_name: str,
    spec: ModelSpec,
    out_dir,
    source: str | None = None,
    **kwargs,
) -> tuple[str, BackendResult]:
    requested = (backend_name or "auto").lower()
    mode = (getattr(spec, "mode", "auto") or "auto").lower()

    if requested == "auto":
        name = resolve_auto(spec, source)
    else:
        name = requested
        violation = mode_violation(mode, name)
        if violation:
            return name, BackendResult(ok=False, error=violation,
                                       log=f"spec.mode={mode!r}, backend={name!r}")

    backend = get_backend(name)

    # Central execution policy. Every code-running backend is refused here when
    # execution is disabled, so the setting is a property of the system rather
    # than of one backend remembering to check.
    from ..config import get_settings

    if backend.executes_user_code and not get_settings().allow_code_exec:
        return name, BackendResult(
            ok=False,
            error=(f"code execution is disabled (FORGE3D_ALLOW_CODE_EXEC=false); "
                   f"the {name!r} backend runs caller-supplied code"),
            log=("Use a non-code backend (image, planet, shape, hosted), or enable "
                 "execution if the caller is trusted."),
        )

    return name, backend.generate(spec, out_dir, source=source, **kwargs)
