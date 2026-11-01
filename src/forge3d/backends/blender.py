"""Blender backend — headless bpy scripting for organic, styled and animated work.

Blender is the escape hatch for everything the CAD engines are bad at: bevels
that look right, subdivision surfaces, sculpt-like forms, materials, rigging and
animation. An agent writes a bpy script, this backend runs it with
`blender -b --factory-startup -P script.py` and collects the exported mesh.

The script receives the export path in `sys.argv[-1]` and must export something.
A minimal working script is in examples/blender_gear.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

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
        return shutil.which("blender") is not None

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None, **kwargs) -> BackendResult:
        if not self.available():
            return missing_binary("blender")
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
            "blender", "-b", "--factory-startup",
            "-P", str(script_path),
            "--", str(glb_path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.get("timeout", 600))
        except subprocess.TimeoutExpired:
            return BackendResult(ok=False, error="blender timed out")

        log = (proc.stdout or "") + (proc.stderr or "")
        if not glb_path.exists():
            return BackendResult(ok=False, error="blender produced no export", log=log[-6000:])

        return BackendResult(
            ok=True,
            path=str(glb_path),
            log=log[-4000:],
            extra_outputs={"bpy": str(script_path)},
            meta={"engine": "blender"},
        )
