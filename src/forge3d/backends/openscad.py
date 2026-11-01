"""OpenSCAD backend — declarative CSG for hard-surface, dimensioned parts.

OpenSCAD is the right engine when the answer has to be *exact*: a mounting
bracket with M4 holes at 40mm centres, an enclosure that must fit a PCB, a
jig. It is also the easiest backend for an agent to self-correct, because
errors are plain text with line numbers.
"""

from __future__ import annotations

from pathlib import Path

from ..platform_support import find_binary, install_hint, run
from ..spec import ModelSpec
from .base import Backend, BackendResult, missing_binary

TEMPLATE = """\
// forge3d — generated OpenSCAD source
// $fn controls smoothness: 32 is a good default, 64+ for display pieces.
$fn = {fn};
{body}
"""


class OpenscadBackend(Backend):
    name = "openscad"
    kind = "local"
    requires: tuple[str, ...] = ("openscad",)
    capabilities = {
        "input": ["scad_source", "spec"],
        "deterministic": True,
        "parametric": True,
        "exact_dimensions": True,
        "exports": ["stl", "off", "amf", "3mf", "dxf", "svg"],
        "best_for": ["hard-surface", "brackets", "enclosures", "jigs", "CSG"],
        "weak_for": ["organic forms", "sculpting", "texturing"],
    }

    def available(self) -> bool:
        return find_binary("openscad", env_var="FORGE3D_OPENSCAD") is not None

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None, **kwargs) -> BackendResult:
        binary = find_binary("openscad", env_var="FORGE3D_OPENSCAD")
        if not binary:
            result = missing_binary("openscad")
            result.log = install_hint("openscad")
            return result
        if not source or not source.strip():
            return BackendResult(
                ok=False,
                error="the openscad backend needs `source` — OpenSCAD code",
                log="Pass the .scad body (no need to include the $fn header).",
            )

        fn = int(kwargs.get("fn", 32))
        body = source if "$fn" in source else TEMPLATE.format(fn=fn, body=source)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        scad_path = out_dir / f"{spec.name}.scad"
        stl_path = out_dir / f"{spec.name}.stl"
        scad_path.write_text(body, encoding="utf-8")

        cmd = [binary, "-o", str(stl_path), str(scad_path)]
        for key, value in (kwargs.get("defines") or {}).items():
            cmd += ["-D", f"{key}={value}"]

        proc = run(cmd, timeout=int(kwargs.get("timeout", 300)))
        if proc.timed_out:
            return BackendResult(ok=False, error="openscad timed out", log=proc.output[-2000:])
        if not proc.ok or not stl_path.exists():
            return BackendResult(ok=False, error="openscad failed", log=proc.output[-4000:])
        log = proc.output

        return BackendResult(
            ok=True,
            path=str(stl_path),
            log=log[-4000:],
            extra_outputs={"scad": str(scad_path)},
            meta={"engine": "openscad", "fn": fn, "binary": binary},
        )
