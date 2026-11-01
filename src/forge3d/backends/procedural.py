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
import json
import sys
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


class _Timeout(Exception):
    pass


# Signals a child can die from when a resource limit bites. Names matter for the
# message an agent has to act on: "exit -24" is unreadable, "SIGXCPU (CPU limit)"
# tells you the build was too slow.
_SIGNAL_HINTS = {
    9: ("SIGKILL", "killed by the parent or the OS"),
    11: ("SIGSEGV", "crashed — segmentation fault"),
    15: ("SIGTERM", "terminated"),
    24: ("SIGXCPU", "killed by the CPU time limit — FORGE3D_CODE_TIMEOUT"),
    25: ("SIGXFSZ", "killed by the file-size limit"),
}


def _describe_exit(returncode: int) -> str:
    """Turn a return code into something an agent can act on.

    CI caught this: the CPU rlimit fires as SIGXCPU (-24) *before* the parent's
    wall-clock timeout on a fast machine, so the generic "exit -24" path was hit
    and the message lost the word "killed". Both paths now say what happened.
    """
    if returncode is None:
        return "model code failed in the sandbox (no exit status)"
    if returncode < 0 or returncode > 128:
        signum = -returncode if returncode < 0 else returncode - 128
        name, why = _SIGNAL_HINTS.get(signum, (f"signal {signum}", "terminated by a signal"))
        return f"model code was killed ({name}: {why})"
    return f"model code failed in the sandbox (exit {returncode})"


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
    executes_user_code = True
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

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # subprocess mode: a killable child with rlimits. This is the mode to use
        # whenever the caller is not fully trusted, and the default when serving
        # over HTTP. See docs/security.md.
        if settings.resolved_exec_mode() == "subprocess":
            return self._generate_subprocess(spec, out_dir, source, settings)

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

    def _generate_subprocess(self, spec: ModelSpec, out_dir: Path, source: str,
                             settings) -> BackendResult:
        """Run the model code in a child process that can actually be killed.

        This is the honest answer to "Python cannot kill a thread": a process
        boundary gives real cancellation (the parent kills it on timeout) and real
        resource ceilings (rlimits in the child). Cost: ~1s of interpreter and
        import startup per generation.
        """
        from ..platform_support import atomic_write, run, sandbox_command

        code_path = out_dir / f"{spec.name}_source.py"
        atomic_write(code_path, source)
        glb = out_dir / f"{spec.name}.glb"
        stl = out_dir / f"{spec.name}.stl"

        cmd = [
            sys.executable, "-m", "forge3d.sandbox_runner",
            str(code_path), str(glb), str(stl),
            json.dumps(spec.to_dict()), str(settings.mem_limit_mb),
        ]
        # Wrap in the strongest isolation this machine offers, and record which
        # one actually ran so an audit never has to trust the configuration.
        cmd, isolation = sandbox_command(cmd, out_dir, settings.sandbox)

        # The child gets a scrubbed environment: no FORGE3D_HOSTED_KEY, no cloud
        # credentials, nothing agent code could exfiltrate. The timeout is passed
        # explicitly because the scrub is deny-by-default.
        proc = run(
            cmd,
            timeout=settings.code_timeout,
            cwd=str(out_dir),
            env={"FORGE3D_CODE_TIMEOUT": str(settings.code_timeout),
                 "FORGE3D_MEM_LIMIT_MB": str(settings.mem_limit_mb)},
        )

        if proc.timed_out:
            return BackendResult(
                ok=False,
                error=f"model code exceeded {settings.code_timeout}s and was killed",
                log=(proc.output or "")[-4000:] +
                    f"\nforge3d: child process terminated after {settings.code_timeout}s "
                    "(real cancellation, not abandonment).",
            )

        if not glb.exists():
            return BackendResult(
                ok=False,
                error=_describe_exit(proc.returncode),
                log=(proc.output or "")[-4000:],
            )

        return BackendResult(
            ok=True,
            path=str(glb),
            log=(proc.output or "")[-4000:],
            extra_outputs={"stl": str(stl), "source": str(code_path)},
            meta={"engine": "trimesh", "mode": "subprocess",
                  "mem_limit_mb": settings.mem_limit_mb,
                  "isolation": isolation},
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
