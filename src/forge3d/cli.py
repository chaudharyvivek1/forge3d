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

from . import backends, context, meshio, platform_support, render, repair, validate, viewer
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


def cmd_demo(args) -> int:
    """Generate, validate and view a model with no arguments and no extra software.

    Everything here runs on the base install: no Blender, no OpenSCAD, no API key,
    no network. It exists so `forge3d demo` is a true one-command try-it.
    """
    from . import server

    shape = args.shape or "supershape"
    print(f"forge3d demo — building a {shape}, validating it, and writing a viewer\n")

    out = server.create_shape(
        shape=shape, resolution=args.resolution, name=f"demo-{shape}",
        purpose="game", render_previews=True, make_viewer=True,
    )
    if not out["ok"]:
        print(f"  failed: {out.get('error')}")
        return 1

    card = out["card"]
    print(f"  model      {out['path']}")
    print(f"  triangles  {card['triangles']:,}")
    print(f"  size       {card['extents']}")
    print(f"  watertight {card['watertight']}")
    print(f"  validation {out['validation']['summary']}")
    for name, path in (out.get("previews") or {}).items():
        print(f"  preview    {path}  ({name})")
    if out.get("viewer"):
        print(f"  viewer     {out['viewer']}")
    print("\nOpen the viewer in a browser — it is one self-contained file.")
    print("Then try: forge3d create --shape torus_knot --resolution 128")
    return 0


def cmd_providers(args) -> int:
    from .providers import registry as provider_registry

    providers = provider_registry.list_providers()
    _print({"providers": providers,
            "configured": [p["name"] for p in providers if p["available"]]})
    if args.check:
        results = provider_registry.check_all(args.check if isinstance(args.check, str) else None)
        ready = [r["provider"] for r in results if r.get("ok")]
        _print({"checks": results, "ready": ready})
        print(f"  {len(ready)}/{len(results)} provider(s) ready", file=sys.stderr)
        # Reporting is not a failure. A CI gate should grep `ready`; exiting
        # non-zero here made a status check look like a crash.
        return 0
    if not args.quiet:
        print()
        for p in providers:
            mark = "configured" if p["available"] else f"needs {p['key_env']}"
            verified = "verified" if p["verified"] else "UNVERIFIED"
            print(f"  {p['name']:8} {mark:28} {verified}")
    return 0


def cmd_doctor(args) -> int:
    workdir = Path(args.workdir) if args.workdir else get_settings().ensure_workdir()
    report = platform_support.doctor(workdir)
    _print(report)
    if not args.quiet:
        print()
        for hint in report["hints"]:
            print(f"  ! {hint}")
        print(f"\n  ready: {'yes' if report['ready'] else 'no'}")
    return 0 if report["ready"] else 2


def cmd_plan(args) -> int:
    spec = ModelSpec(
        name=args.name or "model",
        prompt=args.prompt or "",
        purpose=args.purpose,
        units=args.units,
        dimensions=_dims(args.dims),
        style=args.style or "",
        poly_budget=args.budget,
        mode=getattr(args, "mode", "auto"),
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
    # `forge3d create --shape torus_knot` is the path a new user reaches for, and
    # the demo used to advertise `create --prompt ... --backend procedural`, which
    # cannot work: that backend needs source code.
    shape = getattr(args, "shape", None)
    if shape:
        from . import server

        params: dict = {}
        for item in getattr(args, "param", None) or []:
            if "=" not in item:
                print(f"  --param expects key=value, got {item!r}", file=sys.stderr)
                return 2
            key, _, raw = item.partition("=")
            try:
                params[key] = float(raw) if "." in raw else int(raw)
            except ValueError:
                params[key] = raw
        out = server.create_shape(
            shape=shape, params=params,
            resolution=int(getattr(args, "resolution", 96)),
            name=args.name or shape, purpose=args.purpose,
            render_previews=True, make_viewer=True,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    spec = ModelSpec(
        name=args.name or "model",
        prompt=args.prompt or "",
        purpose=args.purpose,
        units=args.units,
        dimensions=_dims(args.dims),
        style=args.style or "",
        poly_budget=args.budget,
        mode=getattr(args, "mode", "auto"),
    )
    source = Path(args.source).read_text(encoding="utf-8", errors="replace") if args.source else None
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

    if not args.http:
        sys.argv = ["forge3d-mcp"]
    else:
        sys.argv = ["forge3d-mcp", "--http", str(args.http)]
        if args.host:
            sys.argv += ["--host", args.host]
        if args.token:
            sys.argv += ["--token", args.token]
        if args.allow_unauthenticated:
            sys.argv.append("--allow-unauthenticated")
    server_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge3d", description="3D model creation for AI agents")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("backends", help="list geometry engines").set_defaults(func=cmd_backends)

    demo = sub.add_parser("demo", help="one-command try-it: build, validate, view")
    demo.add_argument("--shape", default="supershape",
                      help="any name from `list_shapes` (default: supershape)")
    demo.add_argument("--resolution", type=int, default=96)
    demo.set_defaults(func=cmd_demo)

    prov = sub.add_parser("providers", help="list text-to-3D providers and their status")
    prov.add_argument("--check", nargs="?", const=True, default=False,
                      help="make a live authenticated request (optionally for one provider)")
    prov.add_argument("--quiet", action="store_true", help="JSON only")
    prov.set_defaults(func=cmd_providers)

    doc = sub.add_parser("doctor", help="diagnose this machine: engines, packages, workdir")
    doc.add_argument("--workdir", default=None)
    doc.add_argument("--quiet", action="store_true", help="JSON only, no hints")
    doc.set_defaults(func=cmd_doctor)

    def add_spec_args(sp):
        sp.add_argument("--prompt", default="", help="what the model is")
        sp.add_argument("--name", default="model")
        sp.add_argument("--purpose", default="any", choices=list(validate.PROFILES))
        sp.add_argument("--units", default="mm")
        sp.add_argument("--dims", default=None, help="x=180,y=90,z=75")
        sp.add_argument("--style", default="")
        sp.add_argument("--budget", type=int, default=None)
        sp.add_argument("--mode", default="auto", choices=["auto", "local", "cloud"],
                        help="local never calls a paid provider; cloud uses hosted only")

    plan = sub.add_parser("plan", help="build a checkable spec from a prompt")
    add_spec_args(plan)
    plan.set_defaults(func=cmd_plan)

    create = sub.add_parser("create", help="generate a model")
    add_spec_args(create)
    create.add_argument("--backend", default="auto")
    create.add_argument("--shape", default=None,
                        help="build from the equation library instead (see `list_shapes`)")
    create.add_argument("--param", action="append", metavar="KEY=VALUE",
                        help="a parameter for --shape, repeatable")
    create.add_argument("--resolution", type=int, default=96)
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
    sv.add_argument("--host", default=None,
                    help="bind address (default 127.0.0.1; a public host requires a token)")
    sv.add_argument("--token", default=None,
                    help="bearer token required by HTTP clients (or set FORGE3D_HTTP_TOKEN)")
    sv.add_argument("--allow-unauthenticated", action="store_true",
                    help="permit a public bind with no token — understand the risk first")
    sv.set_defaults(func=cmd_serve)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (meshio.MeshError, FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
        # A mistyped path is a user error, not a crash: print one line, not 30
        # frames of traceback the user has to read past.
        print(f"forge3d: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("forge3d: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
