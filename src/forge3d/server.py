"""The MCP surface — one server, thirteen tools, any agent.

Design rules this file follows, because they are what make an MCP server usable
by a *generic* agent rather than only by its author:

  1. Every tool returns a small JSON dict, never geometry. Paths, counts,
     verdicts and hints — that is all an agent needs, and it keeps the context
     window intact.
  2. Every failure returns `ok: false` plus a `hint` naming the next action.
     An agent that gets a hint can self-correct; one that gets a stack trace
     cannot.
  3. `create_model` is the one-shot happy path. Everything else is a knob for
     when it is not enough.

Run:  forge3d-mcp              (stdio, for Claude Desktop / Cursor / any client)
      forge3d-mcp --http 8000  (streamable HTTP, for remote agents and apps)
"""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import FastMCP

from . import backends, context, meshio, render, repair, validate, viewer
from . import handoff as handoff_mod
from .config import get_settings
from .jobs import JobStore
from .spec import ModelSpec

mcp = FastMCP(
    "forge3d",
    instructions=(
        "3D model creation for AI agents. Start with plan_model to turn a vague request into a "
        "checkable spec, then create_model. Always inspect the returned card and validation "
        "report before telling a user the model is ready. Never paste geometry into context — "
        "pass paths."
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _store() -> JobStore:
    return JobStore(get_settings().ensure_workdir() / "jobs")


def _models_dir() -> Path:
    d = get_settings().ensure_workdir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spec(**kwargs) -> ModelSpec:
    dims = kwargs.get("dimensions")
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except json.JSONDecodeError:
            dims = None
    return ModelSpec(
        name=kwargs.get("name") or "model",
        prompt=kwargs.get("prompt") or "",
        purpose=kwargs.get("purpose") or "any",
        style=kwargs.get("style") or "",
        units=kwargs.get("units") or "mm",
        dimensions=dims,
        poly_budget=kwargs.get("poly_budget"),
        materials=list(kwargs.get("materials") or []),
        constraints=list(kwargs.get("constraints") or []),
        references=list(kwargs.get("references") or []),
        seed=kwargs.get("seed"),
    )


def _need(path: str) -> tuple[Path, dict | None]:
    p = Path(path)
    if not p.exists():
        return p, {
            "ok": False,
            "error": f"no such file: {path}",
            "hint": "use an absolute path from create_model's `path`, or list the workdir first.",
        }
    return p, None


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
@mcp.tool
def list_backends() -> dict:
    """List every geometry engine with its availability, strengths and weaknesses.

    Call this first when a model needs something other than the default. The
    `available` flag is ground truth for this machine — do not guess.
    """
    return {"ok": True, "backends": backends.list_backends()}


@mcp.tool
def plan_model(
    prompt: str = "",
    name: str = "model",
    purpose: str = "any",
    units: str = "mm",
    dimensions: dict | None = None,
    style: str = "",
    poly_budget: int | None = None,
    references: list[str] | None = None,
) -> dict:
    """Turn a vague request into a checkable model spec, before generating anything.

    purpose is print | web | game | cad | any and it changes what "correct"
    means. dimensions are real-world sizes in `units`, e.g. {"x": 180, "y": 90,
    "z": 75}. The response lists what is still missing — ask the user for those
    instead of inventing them.
    """
    spec = _spec(
        prompt=prompt, name=name, purpose=purpose, units=units, dimensions=dimensions,
        style=style, poly_budget=poly_budget, references=references,
    )
    picked = backends.resolve_auto(spec, purpose_hint=purpose)
    backend = backends.get_backend(picked)
    checklist = handoff_mod.new_checklist(
        "print" if spec.purpose == "print" else "web" if spec.purpose == "web" else
        "game" if spec.purpose == "game" else "brief"
    )
    return {
        "ok": True,
        "spec": spec.to_dict(),
        "summary": spec.summary(),
        "missing": spec.missing(),
        "recommended_backend": picked,
        "backend_available": backend.available(),
        "why": f"{picked} is the best fit for purpose={spec.purpose} on this machine",
        "checklist": checklist,
        "next": (
            "ask the user for the missing fields, then call create_model with the same spec"
            if spec.missing()
            else "call create_model"
        ),
    }


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------
@mcp.tool
def create_model(
    prompt: str = "",
    name: str = "model",
    purpose: str = "any",
    backend: str = "auto",
    source: str | None = None,
    units: str = "mm",
    dimensions: dict | None = None,
    style: str = "",
    poly_budget: int | None = None,
    constraints: list[str] | None = None,
    references: list[str] | None = None,
    scale_to_spec: bool = False,
    render_previews: bool = True,
    make_viewer: bool = True,
) -> dict:
    """Generate a 3D model and return its card, validation report and viewer link.

    backend='auto' routes from the spec; pass an explicit engine when you know
    better. `source` is the code that builds the model for the code backends:
      procedural — python defining build() -> trimesh object
      openscad   — OpenSCAD CSG source
      blender    — a bpy script
      cadquery   — python assigning `result`
      hosted     — optional prompt override for a text-to-3D service

    Returns `path` (the mesh), `viewer` (a single-file HTML page you can hand to
    anyone) and `validation`. Always read `validation.ok` before calling it done.
    """
    spec = _spec(
        prompt=prompt, name=name, purpose=purpose, units=units, dimensions=dimensions,
        style=style, poly_budget=poly_budget, constraints=constraints, references=references,
    )
    store = _store()
    job = store.create(backend, spec.to_dict())
    job.status = "running"
    store.save(job)

    out_dir = _models_dir() / spec.name
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        used, result = backends.run(backend, spec, out_dir, source=source)
    except Exception as exc:  # never leak a stack trace to the agent
        store.fail(job, str(exc))
        return {"ok": False, "job_id": job.id, "error": str(exc), "hint": "check the backend name with list_backends"}

    if not result.ok:
        store.fail(job, result.error or "generation failed")
        return {
            "ok": False,
            "job_id": job.id,
            "backend": used,
            "error": result.error,
            "log": result.log,
            "hint": (
                "read `log` — it usually names the exact syntax error or missing binary. "
                "Fix the source and call create_model again with the same spec."
            ),
        }

    path = Path(result.path)
    try:
        obj = meshio.load(path)
        if scale_to_spec:
            obj = validate.scale_to_spec(obj, spec)
            path = meshio.export(obj, path)
    except Exception as exc:
        store.fail(job, f"could not re-open output: {exc}")
        return {"ok": False, "job_id": job.id, "error": f"generated file is unreadable: {exc}"}

    report = validate.validate(obj, profile=spec.purpose, spec=spec)
    previews: dict[str, str] = {}
    if render_previews:
        previews = render.render_views(obj, out_dir / "previews", name=spec.name).get("previews", {})

    html_path = None
    if make_viewer:
        try:
            html_path = str(viewer.build_viewer(path, out_dir / f"{spec.name}.html", spec=spec,
                                                meta={"validation": report.summary()}))
        except Exception:
            html_path = None

    card = context.model_card(path, spec=spec, previews=previews, include_validation=False)
    card["validation"] = {"ok": report.ok, "summary": report.summary(),
                          "failed": [c.id for c in report.checks if not c.ok]}

    store.finish(job, str(path), log=result.log, meta={"backend": used, "faces": card.get("triangles")})

    return {
        "ok": True,
        "job_id": job.id,
        "backend": used,
        "path": str(path),
        "extra_outputs": result.extra_outputs,
        "viewer": html_path,
        "previews": previews,
        "card": card,
        "validation": report.to_dict(),
        "next": (
            "validation passed — offer the viewer link to the user"
            if report.ok
            else "validation failed: call repair_model, then validate_model again"
        ),
    }


@mcp.tool
def get_job(job_id: str) -> dict:
    """Read one generation job by id (status, output path, log, error)."""
    job = _store().get(job_id)
    if job is None:
        return {"ok": False, "error": f"no job {job_id}", "hint": "call list_jobs to see recent ids"}
    return {"ok": True, "job": job.to_dict()}


@mcp.tool
def list_jobs(limit: int = 20) -> dict:
    """Recent generation jobs, newest first."""
    jobs = _store().list(limit=limit)
    return {
        "ok": True,
        "count": len(jobs),
        "jobs": [
            {"id": j.id, "backend": j.backend, "status": j.status, "output": j.output,
             "error": j.error, "name": j.spec.get("name")}
            for j in jobs
        ],
    }


# ---------------------------------------------------------------------------
# inspection and quality
# ---------------------------------------------------------------------------
@mcp.tool
def inspect_model(path: str) -> dict:
    """Geometry facts for a model file: triangles, dimensions, watertightness, bodies.

    Cheap and safe on broken meshes. Use this instead of opening the file.
    """
    p, err = _need(path)
    if err:
        return err
    try:
        obj = meshio.load(p)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "hint": "convert_model can sometimes recover a malformed file"}
    return {"ok": True, "stats": meshio.stats(obj), "card": context.model_card(p)}


@mcp.tool
def validate_model(path: str, profile: str = "any", spec: dict | None = None) -> dict:
    """Check a mesh against a delivery profile: print, web, game, cad or any.

    Each failure carries a `hint`. `ok` is false only when an error-severity
    check fails — warnings are judgement calls, errors are blockers.
    """
    p, err = _need(path)
    if err:
        return err
    try:
        obj = meshio.load(p)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    report = validate.validate(obj, profile=profile, spec=ModelSpec.from_dict(spec) if spec else None)
    return {"ok": True, "report": report.to_dict(), "verdict": report.summary()}


@mcp.tool
def repair_model(
    path: str,
    out_path: str | None = None,
    fill_holes: bool = True,
    fix_normals: bool = True,
    decimate_to: int | None = None,
) -> dict:
    """Merge, drop, fill, re-orient — and optionally decimate — a broken mesh.

    Writes a new file (never overwrites the input unless you pass the same
    path). Returns the actions taken so you can tell a human what changed.
    """
    p, err = _need(path)
    if err:
        return err
    try:
        obj = meshio.load(p)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        mesh, actions = repair.repair(
            obj, fill_holes=fill_holes, fix_normals=fix_normals, decimate_to=decimate_to
        )
    except Exception as exc:
        return {"ok": False, "error": f"repair failed: {exc}", "hint": "try fill_holes=false first"}
    dst = Path(out_path) if out_path else p.with_name(p.stem + "_repaired.glb")
    out = meshio.export(mesh, dst)
    return {
        "ok": True,
        "path": str(out),
        "actions": actions,
        "card": context.model_card(out),
    }


@mcp.tool
def convert_model(path: str, fmt: str = "glb", out_path: str | None = None) -> dict:
    """Convert between mesh formats: stl, obj, ply, off, glb, gltf, dae.

    Delivery cheat-sheet: print -> stl, web/game -> glb, archival/CAD -> step
    (step needs the cadquery backend, not this tool).
    """
    p, err = _need(path)
    if err:
        return err
    try:
        obj = meshio.load(p)
        dst = Path(out_path) if out_path else p.with_name(p.stem).with_suffix(f".{fmt.lstrip('.')}")
        out = meshio.export(obj, dst, fmt=fmt)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "hint": "check the format against the supported list"}
    return {"ok": True, "path": str(out), "format": out.suffix.lstrip("."), "card": context.model_card(out)}


@mcp.tool
def render_model(path: str, views: list[str] | None = None, size: int = 720) -> dict:
    """Render CPU preview images (iso, front, right, top) so you can see the model.

    Use this after every generation. A model you have not looked at is a guess.
    """
    p, err = _need(path)
    if err:
        return err
    try:
        obj = meshio.load(p)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    out_dir = get_settings().ensure_workdir() / "previews"
    result = render.render_views(obj, out_dir, name=p.stem, size=size,
                                views=tuple(views or ("iso", "front", "right", "top")))
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "hint": "pip install matplotlib"}
    return {"ok": True, "previews": result["previews"]}


@mcp.tool
def make_viewer(path: str, out_html: str | None = None, title: str | None = None, spec: dict | None = None) -> dict:
    """Build a single self-contained Three.js HTML page with the model embedded.

    The page works offline and needs no server — send the file to anyone. For
    embedding inside an existing web app, use the `forge3d-viewer` custom
    element instead (viewer_assets/forge3d-viewer.js).
    """
    p, err = _need(path)
    if err:
        return err
    dst = Path(out_html) if out_html else get_settings().ensure_workdir() / "viewer" / f"{p.stem}.html"
    try:
        out = viewer.build_viewer(p, dst, title=title, spec=ModelSpec.from_dict(spec) if spec else None)
    except Exception as exc:
        return {"ok": False, "error": f"could not build viewer: {exc}"}
    return {"ok": True, "html": str(out), "size_kb": round(out.stat().st_size / 1024, 1)}


@mcp.tool
def model_card(path: str) -> dict:
    """A ~300-token summary of a model file — the only safe way to describe it in context.

    Never read mesh data directly; call this and pass the path onward.
    """
    p, err = _need(path)
    if err:
        return err
    return {"ok": True, "card": context.model_card(p), "markdown": context.card_markdown(context.model_card(p))}


# ---------------------------------------------------------------------------
# collaboration
# ---------------------------------------------------------------------------
@mcp.tool
def checklist(name: str = "brief") -> dict:
    """Get a review checklist: brief, geometry, print, web, game or delivery.

    Tick items as they are verified and carry the dict into the handoff.
    """
    try:
        items = handoff_mod.new_checklist(name)
    except KeyError as exc:
        return {"ok": False, "error": str(exc), "hint": f"choose from {sorted(handoff_mod.CHECKLISTS)}"}
    return {"ok": True, "name": name, "items": items, "progress": handoff_mod.checklist_progress(items)}


@mcp.tool
def create_handoff(
    spec: dict,
    artifacts: dict | None = None,
    backend: str | None = None,
    checks: dict | None = None,
    notes: list[str] | None = None,
    owners: dict | None = None,
    next_agent: str | None = None,
    out_path: str | None = None,
) -> dict:
    """Write a handoff packet (JSON + Markdown) so the next agent or teammate needs no context.

    owners maps a role to a name, e.g. {"design": "priya", "geometry": "agent-a"}.
    """
    model_spec = ModelSpec.from_dict(spec)
    packet = handoff_mod.build_handoff(
        model_spec,
        artifacts or {},
        backend=backend,
        checks=checks or {},
        notes=notes or [],
        owners=owners or {},
        next_agent=next_agent,
    )
    dst = Path(out_path) if out_path else get_settings().ensure_workdir() / "handoffs" / f"{packet.id}.json"
    packet.save(dst)
    md = dst.with_suffix(".md")
    md.write_text(packet.to_markdown(), encoding="utf-8")
    return {"ok": True, "id": packet.id, "json": str(dst), "markdown": str(md),
            "open_questions": packet.open_questions}


@mcp.tool
def review_model(path: str, spec: dict | None = None, previews: bool = True) -> dict:
    """One call for the whole review loop: validate + render + card.

    Use this as the last step before telling a user the model is ready, and
    after every repair.
    """
    p, err = _need(path)
    if err:
        return err
    model_spec = ModelSpec.from_dict(spec) if spec else None
    try:
        obj = meshio.load(p)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    report = validate.validate(obj, profile=(model_spec.purpose if model_spec else "any"), spec=model_spec)
    previews_out = {}
    if previews:
        previews_out = render.render_views(obj, get_settings().ensure_workdir() / "previews", name=p.stem).get("previews", {})
    card = context.model_card(p, spec=model_spec, previews=previews_out, include_validation=False)
    return {
        "ok": report.ok,
        "verdict": report.summary(),
        "validation": report.to_dict(),
        "card": card,
        "previews": previews_out,
        "next": "ship it" if report.ok else "repair_model, then review_model again",
    }


def main() -> None:
    import sys

    argv = sys.argv[1:]
    if "--http" in argv:
        idx = argv.index("--http")
        port = int(argv[idx + 1]) if len(argv) > idx + 1 and argv[idx + 1].isdigit() else 8000
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
