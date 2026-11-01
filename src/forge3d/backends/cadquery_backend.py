"""CadQuery backend — real B-rep CAD, and the only route to STEP.

If the model has to go into a CAD/CAM pipeline, a mesh is the wrong output:
it loses the analytic surfaces. CadQuery produces a solid you can export as
STEP, which is what a manufacturer or a downstream CAD user actually wants.

The agent's code must assign a CadQuery Workplane or Shape to `result`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import get_settings
from ..platform_support import atomic_write, run, sandbox_command
from ..spec import ModelSpec
from .base import Backend, BackendResult, missing_binary

RUNNER = """\
import sys
from pathlib import Path

src_path, out_step, out_stl, tolerance = sys.argv[1:5]

import cadquery as cq
from forge3d.sandbox_runner import apply_limits

import os
limits = apply_limits(int(os.environ.get("FORGE3D_MEM_LIMIT_MB", "0")))
print("forge3d-runner: limits:", limits, file=sys.stderr)

namespace = {"cq": cq, "cadquery": cq, "__name__": "forge3d_cad"}
exec(compile(Path(src_path).read_text(encoding="utf-8"), "<forge3d-cadquery>", "exec"), namespace)

result = namespace.get("result")
if result is None:
    print("forge3d-runner: the code must assign `result`", file=sys.stderr)
    sys.exit(3)
if hasattr(result, "val"):
    result = result.val()

cq.exporters.export(result, out_step)
cq.exporters.export(result, out_stl, tolerance=float(tolerance))
print("forge3d-runner: ok")
"""


class CadQueryBackend(Backend):
    name = "cadquery"
    executes_user_code = True
    kind = "local"
    requires: tuple[str, ...] = ("cadquery",)
    capabilities = {
        "input": ["python_source", "spec"],
        "deterministic": True,
        "parametric": True,
        "brep": True,
        "exports": ["step", "stl", "3mf", "dxf", "svg"],
        "best_for": ["manufacturing", "STEP deliverables", "fillets and chamfers", "assemblies"],
        "weak_for": ["organic forms", "texturing"],
    }

    def available(self) -> bool:
        try:
            import cadquery  # noqa: F401
        except Exception:
            return False
        return True

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None, **kwargs) -> BackendResult:
        if not self.available():
            return missing_binary("cadquery (pip install cadquery)")
        if not source or not source.strip():
            return BackendResult(
                ok=False,
                error="the cadquery backend needs `source` — python that assigns `result`",
                log="Expected: import cadquery as cq; result = cq.Workplane('XY').box(10,10,10)",
            )

        settings = get_settings()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        step_path = out_dir / f"{spec.name}.step"
        stl_path = out_dir / f"{spec.name}.stl"

        # Subprocess mode: a killable child with rlimits, exactly like the
        # procedural backend. In-process exec was the one code path that ignored
        # FORGE3D_EXEC_MODE entirely.
        if settings.resolved_exec_mode() == "subprocess":
            return self._generate_subprocess(spec, out_dir, source, step_path, stl_path,
                                             kwargs.get("tolerance", 0.05), settings)

        import cadquery as cq

        namespace: dict = {"cq": cq, "cadquery": cq, "spec": spec, "__name__": "forge3d_cad"}
        try:
            exec(compile(source, "<forge3d-cadquery>", "exec"), namespace)  # noqa: S102
        except Exception as exc:
            return BackendResult(ok=False, error=f"cadquery code raised: {exc}")

        result = namespace.get("result")
        if result is None:
            return BackendResult(ok=False, error="cadquery code must assign `result`")
        if hasattr(result, "val"):
            result = result.val()
        if hasattr(result, "wrapped") is False and not hasattr(result, "exportStep"):
            return BackendResult(ok=False, error=f"`result` is not a CadQuery shape: {type(result)!r}")

        try:
            cq.exporters.export(result, str(step_path))
            cq.exporters.export(result, str(stl_path), tolerance=float(kwargs.get("tolerance", 0.05)))
        except Exception as exc:
            return BackendResult(ok=False, error=f"cadquery export failed: {exc}")

        return BackendResult(
            ok=True,
            path=str(stl_path),
            extra_outputs={"step": str(step_path)},
            meta={"engine": "cadquery", "brep": True, "mode": "inprocess"},
        )

    def _generate_subprocess(self, spec, out_dir, source, step_path, stl_path,
                             tolerance, settings) -> BackendResult:
        """Run CadQuery code in a child process that can be killed."""
        script_path = out_dir / f"{spec.name}_cadquery_runner.py"
        code_path = out_dir / f"{spec.name}_source.py"
        atomic_write(script_path, RUNNER)
        atomic_write(code_path, source)

        cmd = [sys.executable, str(script_path), str(code_path),
               str(step_path), str(stl_path), str(float(tolerance))]
        cmd, isolation = sandbox_command(cmd, out_dir, settings.sandbox)
        proc = run(cmd, timeout=settings.code_timeout, cwd=str(out_dir),
                   env={"FORGE3D_CODE_TIMEOUT": str(settings.code_timeout),
                        "FORGE3D_MEM_LIMIT_MB": str(settings.mem_limit_mb)})

        if proc.timed_out:
            return BackendResult(
                ok=False,
                error=f"cadquery code exceeded {settings.code_timeout}s and was killed",
                log=(proc.output or "")[-4000:],
            )
        if not step_path.exists():
            from .procedural import _describe_exit

            return BackendResult(ok=False, error=_describe_exit(proc.returncode),
                                 log=(proc.output or "")[-4000:])

        return BackendResult(
            ok=True,
            path=str(stl_path),
            extra_outputs={"step": str(step_path), "source": str(code_path)},
            log=(proc.output or "")[-4000:],
            meta={"engine": "cadquery", "brep": True, "mode": "subprocess",
                  "isolation": isolation},
        )
