"""The child process that runs agent-written model code in `subprocess` mode.

Kept in its own module rather than as a string inside `procedural.py` so it can
be read, linted and tested like real code — it is the piece that has to be right.

Contract:
    python -m forge3d.sandbox_runner <code_path> <out.glb> <out.stl> <spec_json> <mem_mb>

Exit codes: 0 ok · 3 no build() · 4 build() raised · 5 bad geometry · 6 export failed
The parent only trusts the output files and the exit code, never stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

EXIT_NO_BUILD = 3
EXIT_BUILD_RAISED = 4
EXIT_BAD_GEOMETRY = 5
EXIT_EXPORT_FAILED = 6


def _apply_limits(mem_mb: int) -> list[str]:
    """Best-effort resource ceilings. Returns what was applied, for the log.

    POSIX only: Windows has no `resource` module, so there the parent's hard kill
    is the only limit. Applied *after* heavy imports on purpose — RLIMIT_AS set
    before numpy loads will fail the import instead of the runaway code.
    """
    applied: list[str] = []
    try:
        import resource
    except ImportError:
        return ["rlimits unavailable on this platform (wall-clock kill only)"]

    if mem_mb > 0:
        limit = mem_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            applied.append(f"address space {mem_mb} MiB")
        except (ValueError, OSError) as exc:
            applied.append(f"address-space limit not applied ({exc})")

    try:
        cpu = max(1, int(os.environ.get("FORGE3D_CODE_TIMEOUT", "120")))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        applied.append(f"cpu {cpu}s")
    except (ValueError, OSError):
        pass

    try:
        # No core dumps.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        applied.append("no core dumps")
    except (ValueError, OSError):
        pass

    try:
        # Bound what the child can write: a runaway loop that appends to a file
        # should hit a wall, not fill the disk. 512 MiB is far more than any
        # mesh export needs.
        fsize = int(os.environ.get("FORGE3D_MAX_FILE_MB", "512")) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        applied.append(f"file size {fsize // (1024 * 1024)} MiB")
    except (ValueError, OSError):
        pass

    try:
        # File descriptors: enough for trimesh's temp files, not enough to hold
        # a thousand sockets open.
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        applied.append("256 file descriptors")
    except (ValueError, OSError):
        pass

    return applied


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 5:
        print("usage: sandbox_runner <code> <out.glb> <out.stl> <spec_json> <mem_mb>",
              file=sys.stderr)
        return 2

    code_path, out_glb, out_stl, spec_json, mem_mb_raw = argv[:5]
    mem_mb = int(mem_mb_raw or 0)

    # Heavy imports first, then the ceilings.
    import math

    import numpy as np
    import trimesh

    from .spec import ModelSpec

    limits = _apply_limits(mem_mb)
    print(f"forge3d-runner: limits: {', '.join(limits)}", file=sys.stderr)

    spec = ModelSpec.from_dict(json.loads(spec_json))

    namespace: dict = {
        "__name__": "forge3d_model",
        "trimesh": trimesh,
        "np": np,
        "numpy": np,
        "math": math,
        "spec": spec,
        "box": lambda x, y, z: trimesh.creation.box(extents=[x, y, z]),
        "cyl": lambda r, h: trimesh.creation.cylinder(radius=r, height=h),
    }

    try:
        source = Path(code_path).read_text(encoding="utf-8")
        exec(compile(source, "<forge3d-procedural>", "exec"), namespace)  # noqa: S102
    except BaseException:
        print("forge3d-runner: model code raised while loading\n" + traceback.format_exc(),
              file=sys.stderr)
        return EXIT_BUILD_RAISED

    build = namespace.get("build")
    if not callable(build):
        print("forge3d-runner: model code must define a callable build()", file=sys.stderr)
        return EXIT_NO_BUILD

    try:
        result = build()
    except BaseException:
        print("forge3d-runner: build() raised\n" + traceback.format_exc(), file=sys.stderr)
        return EXIT_BUILD_RAISED

    # Reuse the parent's coercion rules so both exec modes behave identically.
    from .backends.procedural import ProceduralBackend
    from .meshio import as_mesh

    obj = ProceduralBackend._coerce(result)
    if obj is None:
        print("forge3d-runner: build() returned no usable geometry "
              "(expected Trimesh, Scene, or a list of them)", file=sys.stderr)
        return EXIT_BAD_GEOMETRY

    try:
        obj.export(str(out_glb))
        flat = obj if isinstance(obj, trimesh.Trimesh) else as_mesh(obj)
        flat.export(str(out_stl))
    except BaseException:
        print("forge3d-runner: export failed\n" + traceback.format_exc(), file=sys.stderr)
        return EXIT_EXPORT_FAILED

    print("forge3d-runner: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
