"""Multi-agent and multi-human handoff.

The failure mode with several agents (or a designer and a 3D artist) on one
model is silent divergence: agent B regenerates from a prompt agent A already
superseded, or a reviewer approves a mesh that never passed the print profile.

A `Handoff` is the fix: one JSON object that names the spec, the artifacts, the
checks that passed, the open questions, and who is next. It is small enough to
paste into any context window and complete enough that nobody has to guess.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .spec import ModelSpec

STATUSES = ("draft", "in_review", "blocked", "approved", "shipped")

CHECKLISTS: dict[str, list[str]] = {
    "brief": [
        "purpose chosen (print / web / game / cad)",
        "real-world dimensions in mm recorded",
        "style reference captured or explicitly waived",
        "polygon budget set",
        "commercial-use / licence of any reference or hosted output confirmed",
    ],
    "geometry": [
        "single source of truth for the mesh (one file, one path)",
        "model is a sensible size and orientation (Y-up for web, Z-up for print)",
        "origin at the base centre, not the world origin",
        "no stray helper objects, cameras or lights in the export",
        "scale applied (no non-uniform object scale left in the transform)",
    ],
    "print": [
        "watertight (validation: watertight)",
        "consistent winding, positive volume",
        "minimum wall thickness respected for the target process",
        "overhangs reviewed for the chosen orientation",
        "exported as binary STL or 3MF at 1:1 scale",
    ],
    "web": [
        "exported as GLB, textures embedded",
        "triangle count within budget for the target device",
        "UVs present and materials use PBR metallic-roughness",
        "viewer tested on a phone-sized viewport",
        "file size acceptable (roughly under 5 MB for a hero asset)",
    ],
    "game": [
        "LOD chain generated",
        "collision proxy is simpler than the visual mesh",
        "pivot and forward axis match engine convention",
        "material count minimised, texture atlased where possible",
        "rig/skeleton convention agreed if animated",
    ],
    "delivery": [
        "final export path recorded in the handoff",
        "validation report attached and passing for the target profile",
        "preview images attached",
        "named owner for the next step",
        "licence and attribution note written",
    ],
}


@dataclass
class Handoff:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = "untitled model task"
    status: str = "draft"
    spec: dict = field(default_factory=dict)
    backend: str | None = None
    artifacts: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    owners: dict = field(default_factory=dict)
    next_agent: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_markdown(self) -> str:
        lines = [f"# Handoff — {self.title}", ""]
        lines.append(f"`{self.id}` · status **{self.status}** · backend `{self.backend or 'unset'}`")
        if self.owners:
            lines.append("")
            lines.append("**Owners**")
            for role, who in self.owners.items():
                lines.append(f"- {role}: {who}")
        if self.spec:
            spec = ModelSpec.from_dict(self.spec)
            lines.append("")
            lines.append("**Spec**")
            lines.append(f"- {spec.summary()}")
            if spec.prompt:
                lines.append(f"- brief: {spec.prompt}")
        if self.artifacts:
            lines.append("")
            lines.append("**Artifacts**")
            for key, value in self.artifacts.items():
                lines.append(f"- {key}: `{value}`")
        if self.checks:
            lines.append("")
            lines.append("**Checks**")
            for key, value in self.checks.items():
                mark = "x" if value else " "
                lines.append(f"- [{mark}] {key}")
        if self.open_questions:
            lines.append("")
            lines.append("**Open questions**")
            for q in self.open_questions:
                lines.append(f"- {q}")
        if self.notes:
            lines.append("")
            lines.append("**Notes**")
            for n in self.notes:
                lines.append(f"- {n}")
        if self.next_agent:
            lines.append("")
            lines.append(f"**Next:** {self.next_agent}")
        return "\n".join(lines)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> Handoff:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})


def new_checklist(name: str) -> dict:
    if name not in CHECKLISTS:
        raise KeyError(f"unknown checklist {name!r}; choose from {sorted(CHECKLISTS)}")
    return dict.fromkeys(CHECKLISTS[name], False)


def checklist_progress(checks: dict) -> dict:
    total = len(checks)
    done = sum(1 for v in checks.values() if v)
    return {
        "total": total,
        "done": done,
        "percent": round(100 * done / total, 1) if total else 0.0,
        "remaining": [k for k, v in checks.items() if not v],
    }


def build_handoff(spec: ModelSpec, artifacts: dict, backend: str | None = None, **kwargs) -> Handoff:
    """Assemble a handoff from a finished generation step."""
    return Handoff(
        title=kwargs.pop("title", spec.name),
        spec=spec.to_dict(),
        backend=backend,
        artifacts=artifacts,
        checks=kwargs.pop("checks", {}),
        notes=kwargs.pop("notes", []),
        open_questions=kwargs.pop("open_questions", spec.missing()),
        owners=kwargs.pop("owners", {}),
        next_agent=kwargs.pop("next_agent", None),
    )
