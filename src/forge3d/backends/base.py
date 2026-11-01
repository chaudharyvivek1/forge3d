"""The backend contract.

A backend is anything that can turn a `ModelSpec` (plus optional source code or
a source file) into a mesh on disk. Keeping this interface tiny is the whole
point: an agent should be able to swap Blender for OpenSCAD for a hosted API
without changing its workflow, its validation, or its viewer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..spec import ModelSpec


@dataclass
class BackendResult:
    ok: bool
    path: str | None = None
    log: str = ""
    error: str | None = None
    meta: dict = field(default_factory=dict)
    extra_outputs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "path": self.path,
            "error": self.error,
            "log": self.log[-4000:],
            "meta": self.meta,
            "extra_outputs": self.extra_outputs,
        }


class Backend:
    """Base class. Subclasses set `name`, `kind`, `requires`, `capabilities`."""

    name: str = "base"
    kind: str = "local"          # local | external
    requires: tuple[str, ...] = ()
    capabilities: dict = {}
    #: True when this backend runs code supplied by the caller. The registry
    #: refuses such a backend outright when FORGE3D_ALLOW_CODE_EXEC is false —
    #: previously only the procedural backend checked, so CadQuery and Blender
    #: bypassed the setting entirely.
    executes_user_code: bool = False

    def available(self) -> bool:
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available(),
            "requires": list(self.requires),
            "executes_user_code": self.executes_user_code,
            "capabilities": self.capabilities,
        }

    def generate(
        self,
        spec: ModelSpec,
        out_dir: Path,
        source: str | None = None,
        **kwargs,
    ) -> BackendResult:
        raise NotImplementedError


def missing_binary(name: str) -> BackendResult:
    return BackendResult(
        ok=False,
        error=f"{name} is not installed or not on PATH",
        log=f"Install {name} and re-run, or pass backend='procedural' which needs no external tools.",
    )
