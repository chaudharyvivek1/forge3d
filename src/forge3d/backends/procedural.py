"""Procedural backend — the agent writes Python, trimesh builds the mesh.

This is the default and the most reliable engine for anything with real
dimensions: brackets, furniture, enclosures, parametric parts, repeated
structures, anything you want to be able to *change* later. It needs no
external binary, output is deterministic, and the source is a diffable artifact.

The agent's code must define a function `build()` returning a
`trimesh.Trimesh`, a `trimesh.Scene`, or a list of either. Helpers `trimesh`,
`np`, `math` and `box`/`cyl` are preloaded.
"""

from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np
import trimesh

from .. import meshio
from ..config import get_settings
from ..platform_support import TimeoutExceeded, safe_timeout
from ..spec import ModelSpec
from .base import Backend, BackendResult

PREAMBLE = """\
# helpers available in this namespace: trimesh, np, math, box(x,y,z), cyl(r,h), spec
"""


class _Timeout(Exception):
    pass


def _run_guarded(code: str, namespace: dict, timeout: int):
    """Execute agent code with a wall-clock ceiling that works on every platform.

    This previously used `signal.alarm`, which does not exist on Windows and
    raises `ValueError: signal only works in main thread` elsewhere. MCP servers
    run synchronous tools in a threadpool, so the timeout crashed in exactly the
    environment this backend is meant for. `safe_timeout` is thread-based.

    Capturing stdout matters more than it looks: agent code that prints would
    otherwise write straight into the stdio JSON-RPC stream and corrupt it.
    """
    buf_out, buf_err = io.StringIO(), io.StringIO()

    def _exec():
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exec(compile(code, "<forge3d-procedural>", "exec"), namespace)  # noqa: S102

    safe_timeout(_exec, max(1, int(timeout)), what="model code")
    return buf_out.getvalue(), buf_err.getvalue()


class ProceduralBackend(Backend):
    name = "procedural"
    kind = "local"
    requires: tuple[str, ...] = ("trimesh", "numpy")
    capabilities = {
        "input": ["python_source", "spec"],
        "deterministic": True,
        "parametric": True,
        "units": True,
        "best_for": ["precise dimensions", "repeated structure", "editable output", "offline"],
        "weak_for": ["organic forms", "sculpted detail"],
    }

    def available(self) -> bool:
        return True

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None, **kwargs) -> BackendResult:
        settings = get_settings()
        if not settings.allow_code_exec:
            return BackendResult(
                ok=False,
                error="code execution is disabled (FORGE3D_ALLOW_CODE_EXEC=false)",
                log="Use backend='openscad', 'cadquery' or 'hosted' instead.",
            )
        if not source or not source.strip():
            return BackendResult(
                ok=False,
                error="the procedural backend needs `source` — python that defines build()",
                log="See examples/procedural_chair.py for the expected shape.",
            )

        namespace: dict = {
            "__name__": "forge3d_model",
            "trimesh": trimesh,
            "np": np,
            "numpy": np,
            "math": __import__("math"),
            "spec": spec,
            "box": lambda x, y, z: trimesh.creation.box(extents=[x, y, z]),
            "cyl": lambda r, h: trimesh.creation.cylinder(radius=r, height=h),
        }

        try:
            stdout, stderr = _run_guarded(source, namespace, settings.code_timeout)
        except TimeoutExceeded:
            return BackendResult(
                ok=False,
                error=f"model code exceeded {settings.code_timeout}s",
                log="Reduce the work inside build(), or raise FORGE3D_CODE_TIMEOUT. "
                    "Note: the abandoned thread keeps running until it finishes.",
            )
        except Exception:
            return BackendResult(ok=False, error="model code raised", log=traceback.format_exc()[-4000:])

        build = namespace.get("build")
        if not callable(build):
            return BackendResult(
                ok=False,
                error="model code must define a callable build()",
                log="Expected: def build(): ... return trimesh.Trimesh(...)",
            )

        try:
            result = build()
        except Exception:
            return BackendResult(ok=False, error="build() raised", log=traceback.format_exc()[-4000:])

        result = self._coerce(result)
        if result is None:
            return BackendResult(
                ok=False,
                error="build() returned no usable geometry",
                log="Return a trimesh.Trimesh, trimesh.Scene, or a non-empty list of them.",
            )

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        glb = out_dir / f"{spec.name}.glb"
        stl = out_dir / f"{spec.name}.stl"
        result.export(str(glb))
        # STL is what a printer or a slicer wants; cheap to write alongside.
        flat = result if isinstance(result, trimesh.Trimesh) else meshio.as_mesh(result)
        flat.export(str(stl))

        return BackendResult(
            ok=True,
            path=str(glb),
            log=(stdout + stderr)[-4000:],
            extra_outputs={"stl": str(stl)},
            meta={"engine": "trimesh", "faces": len(flat.faces)},
        )

    @staticmethod
    def _coerce(result):
        if isinstance(result, (trimesh.Trimesh, trimesh.Scene)):
            return result
        if isinstance(result, (list, tuple)):
            parts = [r for r in result if isinstance(r, (trimesh.Trimesh, trimesh.Scene))]
            if not parts:
                return None
            if len(parts) == 1:
                return parts[0]
            scene = trimesh.Scene()
            for i, part in enumerate(parts):
                if isinstance(part, trimesh.Trimesh):
                    scene.add_geometry(part, node_name=f"part_{i}")
                else:
                    scene.add_geometry(part)
            return scene
        return None
