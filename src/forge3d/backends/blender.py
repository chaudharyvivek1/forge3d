"""Blender backend — headless bpy scripting for organic, styled and animated work.

Blender is the escape hatch for everything the CAD engines are bad at: bevels
that look right, subdivision surfaces, sculpt-like forms, materials, rigging and
animation. An agent writes a bpy script, this backend runs it with
`blender -b --factory-startup -P script.py` and collects the exported mesh.

The script receives the export path in `sys.argv[-1]` and must export something.
A minimal working script is in examples/blender_gear.py.
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_settings
from ..platform_support import find_binary, install_hint, run, sandbox_command
from ..spec import ModelSpec
from .base import Backend, BackendResult, missing_binary

WRAPPER = """\
import bpy, sys, os
OUT = sys.argv[-1]
# agent script follows; it should end by exporting to OUT
{body}
"""

EXPORT_HINT = """\

# ---- forge3d export (auto-appended) ----
if not os.path.exists(OUT):
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.export_scene.gltf(filepath=OUT, export_format='GLB', use_selection=True)
"""


class BlenderBackend(Backend):
    name = "blender"
    executes_user_code = True
    kind = "local"
    requires: tuple[str, ...] = ("blender",)
    capabilities = {
        "input": ["bpy_source", "spec"],
        "deterministic": False,
        "parametric": True,
        "modifiers": True,
        "materials": True,
        "rigging": True,
        "animation": True,
        "exports": ["glb", "gltf", "fbx", "obj", "stl", "usd"],
        "best_for": ["organic forms", "bevels and subdivision", "materials", "animation", "product shots"],
        "weak_for": ["exact mechanical dimensions"],
    }

    def available(self) -> bool:
        return find_binary("blender", env_var="FORGE3D_BLENDER") is not None

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None, **kwargs) -> BackendResult:
        binary = find_binary("blender", env_var="FORGE3D_BLENDER")
        if not binary:
            result = missing_binary("blender")
            result.log = install_hint("blender")
            return result
        if not source or not source.strip():
            return BackendResult(
                ok=False,
                error="the blender backend needs `source` — a bpy python script",
                log="See examples/blender_gear.py.",
            )

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        glb_path = out_dir / f"{spec.name}.glb"
        script_path = out_dir / f"{spec.name}_bpy.py"

        body = source
        if "export_scene" not in body and "export_scene.gltf" not in body:
            body += EXPORT_HINT
        script_path.write_text(WRAPPER.format(body=body), encoding="utf-8")

        cmd = [
            binary, "-b", "--factory-startup",
            "-P", str(script_path),
            "--", str(glb_path),
        ]
        # Blender was the one code-running backend with a timeout but no sandbox.
        settings = get_settings()
        cmd, isolation = sandbox_command(cmd, out_dir, settings.sandbox)
        proc = run(
            cmd,
            # Same budget as the procedural and CadQuery children — 600s here was
            # inconsistent with FORGE3D_CODE_TIMEOUT everywhere else.
            timeout=int(kwargs.get("timeout") or settings.code_timeout),
            cwd=str(out_dir),
            limits=True,
            env={"FORGE3D_CODE_TIMEOUT": str(settings.code_timeout),
                 "FORGE3D_MEM_LIMIT_MB": str(settings.mem_limit_mb)},
        )
        # Report what actually happened. A fallback labelled "rlimit" must not
        # imply limits were applied when the platform cannot apply them.
        if isolation == "rlimit" and not proc.limits_applied:
            isolation = "none (resource limits unavailable on this platform)"
        if proc.timed_out:
            return BackendResult(ok=False, error="blender timed out",
                                 log=proc.output[-2000:],
                                 meta={"isolation": isolation})

        log = proc.output
        if not glb_path.exists():
            return BackendResult(ok=False, error="blender produced no export",
                                 log=log[-6000:], meta={"isolation": isolation})

        return BackendResult(
            ok=True,
            path=str(glb_path),
            log=log[-4000:],
            extra_outputs={"bpy": str(script_path)},
            meta={"engine": "blender", "binary": binary, "isolation": isolation},
        )
