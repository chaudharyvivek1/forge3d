"""CadQuery backend — real B-rep CAD, and the only route to STEP.

If the model has to go into a CAD/CAM pipeline, a mesh is the wrong output:
it loses the analytic surfaces. CadQuery produces a solid you can export as
STEP, which is what a manufacturer or a downstream CAD user actually wants.

The agent's code must assign a CadQuery Workplane or Shape to `result`.
"""

from __future__ import annotations

from pathlib import Path

from ..spec import ModelSpec
from .base import Backend, BackendResult, missing_binary


class CadQueryBackend(Backend):
    name = "cadquery"
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

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        step_path = out_dir / f"{spec.name}.step"
        stl_path = out_dir / f"{spec.name}.stl"

        try:
            cq.exporters.export(result, str(step_path))
            cq.exporters.export(result, str(stl_path), tolerance=float(kwargs.get("tolerance", 0.05)))
        except Exception as exc:
            return BackendResult(ok=False, error=f"cadquery export failed: {exc}")

        return BackendResult(
            ok=True,
            path=str(stl_path),
            extra_outputs={"step": str(step_path)},
            meta={"engine": "cadquery", "brep": True},
        )
