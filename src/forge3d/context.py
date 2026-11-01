"""Context-window discipline for 3D work.

A GLB is megabytes. A mesh's vertex list is hundreds of thousands of tokens. No
agent should ever paste geometry into its context — it should pass a *card*:
a few hundred tokens of facts plus a file path. Every module in forge3d accepts
paths, and this module is the only sanctioned way to describe what is behind
one.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import meshio, validate
from .spec import ModelSpec

CARD_BUDGET_TOKENS = 400


def model_card(
    path: str | Path,
    spec: ModelSpec | None = None,
    previews: dict[str, str] | None = None,
    include_validation: bool = True,
) -> dict:
    """A compact, JSON-safe description of a model file.

    Roughly 200-350 tokens. Deliberately excludes vertex data, normals and
    anything else that scales with geometry.
    """
    p = Path(path)
    card: dict = {
        "file": p.name,
        "path": str(p),
        "exists": p.exists(),
        "size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else None,
        "format": p.suffix.lstrip(".").lower(),
    }

    if not p.exists():
        return card

    try:
        obj = meshio.load(p)
        st = meshio.stats(obj)
    except Exception as exc:
        card["error"] = f"could not inspect: {exc}"
        return card

    card.update(
        {
            "triangles": st["faces"],
            "vertices": st["vertices"],
            "extents": [round(e, 3) for e in st["extents"]],
            "units": (spec.units if spec else "unknown"),
            "watertight": st["is_watertight"],
            "bodies": st["body_count"],
            "volume": round(st["volume"], 3),
        }
    )

    if spec is not None:
        card["spec"] = {
            "name": spec.name,
            "purpose": spec.purpose,
            "style": spec.style,
            "target_dims": spec.extent,
            "poly_budget": spec.poly_budget,
        }
        card["recommended_export"] = meshio.RECOMMENDED.get(spec.purpose, ".glb")

    if include_validation:
        report = validate.validate(obj, profile=(spec.purpose if spec else "any"), spec=spec)
        card["validation"] = {
            "ok": report.ok,
            "summary": report.summary(),
            "failed": [c.id for c in report.checks if not c.ok],
        }

    if previews:
        card["previews"] = previews

    return card


def card_json(card: dict) -> str:
    return json.dumps(card, indent=2)


def card_markdown(card: dict) -> str:
    lines = [f"**{card.get('file', 'model')}** — {card.get('format', '?')}"]
    if "triangles" in card:
        dims = " x ".join(f"{e:g}" for e in card.get("extents", []))
        lines.append(
            f"{card['triangles']:,} tris · {dims} {card.get('units', '')} · "
            f"{'watertight' if card.get('watertight') else 'not watertight'} · "
            f"{card.get('bodies', 0)} body"
        )
    if "validation" in card:
        v = card["validation"]
        lines.append(f"validation: {v['summary']}")
        if v["failed"]:
            lines.append("failing checks: " + ", ".join(v["failed"]))
    if card.get("recommended_export"):
        lines.append(f"ship as: {card['recommended_export']}")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Cheap heuristic — 4 chars per token — so we never call a tokenizer here."""
    return max(1, len(text) // 4)
