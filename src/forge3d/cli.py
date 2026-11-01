"""Command line interface — the same capabilities without an agent.

    forge3d backends
    forge3d plan --prompt "wall bracket for a 32mm pipe" --purpose print --dims x=120,y=60,z=40
    forge3d create --prompt "..." --source model.py --backend procedural --viewer
    forge3d validate model.glb --profile print
    forge3d render model.glb
    forge3d viewer model.glb --out bracket.html
    forge3d convert model.glb --fmt stl
    forge3d card model.glb
    forge3d serve --http 8000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import backends, context, meshio, render, repair, validate, viewer
from .config import get_settings
from .spec import ModelSpec


def _dims(raw: str | None) -> dict | None:
    if not raw:
        return None
    out = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out or None


def _print(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def cmd_backends(_args) -> int:
    _print({"backends": backends.list_backends()})
    return 0


def cmd_plan(args) -> int:
    spec = ModelSpec(
        name=args.name or "model",
        prompt=args.prompt or "",
        purpose=args.purpose,
        units=args.units,
        dimensions=_dims(args.dims),
        style=args.style or "",
        poly_budget=args.budget,
    )
    picked = backends.resolve_auto(spec, purpose_hint=args.purpose)
    _print(
        {
            "spec": spec.to_dict(),
            "summary": spec.summary(),
            "missing": spec.missing(),
            "recommended_backend": picked,
            "available": backends.get_backend(picked).available(),
        }
    )
    return 0


def cmd_create(args) -> int:
    spec = ModelSpec(
        name=args.name or "model",
        prompt=args.prompt or "",
        purpose=args.purpose,
        units=args.units,
        dimensions=_dims(args.dims),
        style=args.style or "",
        poly_budget=args.budget,
    )
    source = Path(args.source).read_text(encoding="utf-8") if args.source else None
    out_dir = get_settings().ensure_workdir() / "models" / spec.name
    used, result = backends.run(args.backend, spec, out_dir, source=source)

    if not result.ok:
        _print({"ok": False, "backend": used, "error": result.error, "log": result.log[-2000:]})
        return 1

    path = Path(result.path)
    obj = meshio.load(path)
    if args.scale_to_spec:
        obj = validate.scale_to_spec(obj, spec)
        path = meshio.export(obj, path)

    report = validate.validate(obj, profile=spec.purpose, spec=spec)
    previews = render.render_views(obj, out_dir / "previews", name=spec.name).get("previews", {})
    html = viewer.build_viewer(path, out_dir / f"{spec.name}.html", spec=spec) if args.viewer else None

    _print(
        {
            "ok": True,
            "backend": used,
            "path": str(path),
            "viewer": str(html) if html else None,
            "previews": previews,
            "verdict": report.summary(),
            "failed": [c.id for c in report.checks if not c.ok],
            "card": context.model_card(path, spec=spec, previews=previews, include_validation=False),
        }
    )
    return 0 if report.ok else 2


def cmd_validate(args) -> int:
    obj = meshio.load(args.path)
    report = validate.validate(obj, profile=args.profile)
    _print(report.to_dict())
    return 0 if report.ok else 2


def cmd_render(args) -> int:
    obj = meshio.load(args.path)
    out_dir = Path(args.out) if args.out else get_settings().ensure_workdir() / "previews"
    result = render.render_views(obj, out_dir, name=Path(args.path).stem)
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_viewer(args) -> int:
    out = Path(args.out) if args.out else get_settings().ensure_workdir() / "viewer" / (Path(args.path).stem + ".html")
    path = viewer.build_viewer(args.path, out, title=args.title)
    _print({"ok": True, "html": str(path), "size_kb": round(path.stat().st_size / 1024, 1)})
    return 0


def cmd_convert(args) -> int:
    out = meshio.convert(args.path, args.out or Path(args.path).with_suffix(f".{args.fmt}"))
    _print({"ok": True, "path": str(out)})
    return 0


def cmd_repair(args) -> int:
    mesh, actions = repair.repair(meshio.load(args.path), decimate_to=args.decimate)
    out = meshio.export(mesh, args.out or Path(args.path).with_name(Path(args.path).stem + "_repaired.glb"))
    _print({"ok": True, "path": str(out), "actions": actions})
    return 0


def cmd_card(args) -> int:
    card = context.model_card(args.path)
    print(context.card_markdown(card))
    return 0


def cmd_serve(args) -> int:
    from .server import main as server_main

    sys.argv = ["forge3d-mcp"] + (["--http", str(args.http)] if args.http else [])
    server_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge3d", description="3D model creation for AI agents")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("backends", help="list geometry engines").set_defaults(func=cmd_backends)

    def add_spec_args(sp):
        sp.add_argument("--prompt", default="", help="what the model is")
        sp.add_argument("--name", default="model")
        sp.add_argument("--purpose", default="any", choices=list(validate.PROFILES))
        sp.add_argument("--units", default="mm")
        sp.add_argument("--dims", default=None, help="x=180,y=90,z=75")
        sp.add_argument("--style", default="")
        sp.add_argument("--budget", type=int, default=None)

    plan = sub.add_parser("plan", help="build a checkable spec from a prompt")
    add_spec_args(plan)
    plan.set_defaults(func=cmd_plan)

    create = sub.add_parser("create", help="generate a model")
    add_spec_args(create)
    create.add_argument("--backend", default="auto")
    create.add_argument("--source", default=None, help="file with backend source code")
    create.add_argument("--scale-to-spec", action="store_true")
    create.add_argument("--viewer", action="store_true", help="also build the Three.js page")
    create.set_defaults(func=cmd_create)

    val = sub.add_parser("validate", help="check against a delivery profile")
    val.add_argument("path")
    val.add_argument("--profile", default="any", choices=list(validate.PROFILES))
    val.set_defaults(func=cmd_validate)

    ren = sub.add_parser("render", help="CPU preview images")
    ren.add_argument("path")
    ren.add_argument("--out", default=None)
    ren.set_defaults(func=cmd_render)

    vw = sub.add_parser("viewer", help="single-file Three.js page")
    vw.add_argument("path")
    vw.add_argument("--out", default=None)
    vw.add_argument("--title", default=None)
    vw.set_defaults(func=cmd_viewer)

    cv = sub.add_parser("convert", help="change mesh format")
    cv.add_argument("path")
    cv.add_argument("--fmt", default="glb")
    cv.add_argument("--out", default=None)
    cv.set_defaults(func=cmd_convert)

    rp = sub.add_parser("repair", help="fix a broken mesh")
    rp.add_argument("path")
    rp.add_argument("--out", default=None)
    rp.add_argument("--decimate", type=int, default=None)
    rp.set_defaults(func=cmd_repair)

    cd = sub.add_parser("card", help="compact model summary")
    cd.add_argument("path")
    cd.set_defaults(func=cmd_card)

    sv = sub.add_parser("serve", help="run the MCP server")
    sv.add_argument("--http", type=int, default=None, help="port; omit for stdio")
    sv.set_defaults(func=cmd_serve)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
