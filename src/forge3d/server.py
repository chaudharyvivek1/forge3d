"""The MCP surface — one server, twenty-one tools, any agent.

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
import sys
import threading
import time
import uuid
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from . import backends, context, meshio, platform_support, render, repair, validate, viewer
from . import handoff as handoff_mod
from .config import get_settings
from .jobs import JobStore
from .spec import ModelSpec  # noqa: F401 - re-exported for callers of this module

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
# --- concurrency ---------------------------------------------------------
# 3D work is CPU-bound. Ten concurrent builds on four cores makes every one of
# them slow, and a slow build looks like a hung server to a client with a
# timeout. One semaphore per configured limit, created lazily so tests can change
# the setting without leaking old slots.
_SLOT_LOCK = threading.Lock()
_SLOTS: dict[int, threading.BoundedSemaphore] = {}


def _slots(limit: int) -> threading.BoundedSemaphore:
    with _SLOT_LOCK:
        return _SLOTS.setdefault(max(1, int(limit)), threading.BoundedSemaphore(max(1, int(limit))))


def _log_event(event: dict) -> None:
    """Structured log line, or nothing, depending on FORGE3D_LOG_JSON."""
    if not get_settings().log_json:
        return
    print(json.dumps({"ts": round(time.time(), 3), **event}), file=sys.stderr, flush=True)


class ToolLogger(Middleware):
    """Timing and outcome for every tool call, as one JSON line each.

    Without this, a production incident has no record of which tool a client
    hammered or which one silently got slow.
    """

    async def on_call_tool(self, context, call_next):
        name = getattr(getattr(context, "message", None), "name", "unknown")
        started = time.perf_counter()
        status = "ok"
        try:
            return await call_next(context)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            _log_event({
                "event": "tool_call",
                "tool": name,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "status": status,
            })


mcp.add_middleware(ToolLogger())


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
        mode=kwargs.get("mode") or "auto",
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
def doctor() -> dict:
    """Diagnose this runtime: OS, Python, which engines exist, what is missing and how to fix it.

    Call this when a backend reports itself unavailable, or before telling a user
    that something cannot be done. It returns copy-pasteable install commands
    for the current OS rather than a generic error.
    """
    settings = get_settings()
    report = platform_support.doctor(settings.ensure_workdir())
    return {"ok": True, "report": report, "hints": report["hints"]}


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
    mode: str = "auto",
) -> dict:
    """Turn a vague request into a checkable model spec, before generating anything.

    purpose is print | web | game | cad | any and it changes what "correct"
    means. dimensions are real-world sizes in `units`, e.g. {"x": 180, "y": 90,
    "z": 75}. The response lists what is still missing — ask the user for those
    instead of inventing them.
    """
    spec = _spec(
        prompt=prompt, name=name, purpose=purpose, units=units, dimensions=dimensions,
        style=style, poly_budget=poly_budget, references=references, mode=mode,
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
    materials: list[str] | None = None,
    seed: int | None = None,
    mode: str = "auto",
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
        materials=materials, seed=seed, mode=mode,
    )
    store = _store()
    store.recover_stale()
    job = store.create(backend, spec.to_dict())
    job.status = "running"
    store.save(job)

    # Job-scoped output directory. Two agents generating "chair" concurrently
    # used to write to the same path and silently overwrite each other; the
    # result is still deterministic per job id, and the full path is returned.
    out_dir = _models_dir() / spec.name / job.id
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    slots = _slots(settings.max_concurrent)
    if not slots.acquire(timeout=settings.queue_wait):
        store.fail(job, "server busy")
        return {
            "ok": False,
            "job_id": job.id,
            "error": f"server busy: {settings.max_concurrent} generations already running",
            "hint": f"retry shortly, or raise FORGE3D_MAX_CONCURRENT / FORGE3D_QUEUE_WAIT. "
                    f"Waited {settings.queue_wait}s for a free slot; an unbounded queue would "
                    f"hide an overload the client needs to see.",
        }
    try:
        used, result = backends.run(backend, spec, out_dir, source=source)
    except Exception as exc:  # never leak a stack trace to the agent
        store.fail(job, str(exc))
        return {"ok": False, "job_id": job.id, "error": str(exc), "hint": "check the backend name with list_backends"}
    finally:
        slots.release()

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

    The mesh is embedded as a data URI, so the page needs no server, no CORS and
    no hosting — send the file to anyone. Note that three.js itself loads from a
    CDN by default: for an air-gapped machine, set FORGE3D_THREE_URL to a local
    mirror before building the page. For embedding inside an existing web app,
    use the `forge3d-viewer` custom element instead (viewer_assets/forge3d-viewer.js).
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
def create_planet(
    radius: float = 100.0,
    seed: int = 0,
    subdivisions: int = 5,
    crater_density: float = 0.55,
    canyon: bool = False,
    canyon_depth: float = 0.05,
    roughness: float = 1.0,
    palette: str = "mars",
    texture_resolution: int = 512,
    name: str = "planet",
    purpose: str = "web",
    poly_budget: int | None = None,
    render_previews: bool = True,
    make_viewer: bool = True,
) -> dict:
    """Generate a procedural rocky planet: terrain, craters, optional canyon, baked PBR maps.

    Deterministic — the same seed always produces the same planet, so a website
    hero image is reproducible. Writes albedo, normal and metallic-roughness
    textures with correct spherical UVs and embeds them in the GLB.

    Use this instead of writing noise code inside `create_model`: the crater
    profile, seamless 3D noise sampling and UV layout are already correct here,
    and a hand-rolled version usually gets at least one of them wrong.

    radius is in world units. subdivisions: 4 = ~5k faces, 5 = ~20k, 6 = ~82k.
    texture_resolution 0 disables baking (vertex colours only, much smaller file).
    """
    from .geometry.planets import PlanetParams, attach_pbr, generate_planet

    store = _store()
    job = store.create("planet", {"name": name, "seed": seed, "radius": radius})
    job.status = "running"
    store.save(job)

    out_dir = _models_dir() / name / job.id
    out_dir.mkdir(parents=True, exist_ok=True)

    params = PlanetParams(
        radius=float(radius), seed=int(seed),
        subdivisions=max(2, min(7, int(subdivisions))),
        crater_density=float(crater_density), canyon=bool(canyon),
        canyon_depth=float(canyon_depth), roughness=float(roughness),
        palette=palette, texture_resolution=int(texture_resolution), name=name,
    )

    try:
        mesh, info = generate_planet(params, out_dir=out_dir)
        if info["textures"]:
            attach_pbr(mesh, info["textures"])
        glb = out_dir / f"{name}.glb"
        stl = out_dir / f"{name}.stl"
        mesh.export(str(glb))
        mesh.export(str(stl))
    except Exception as exc:  # noqa: BLE001 - reported to the agent
        store.fail(job, str(exc))
        return {"ok": False, "job_id": job.id, "error": f"planet generation failed: {exc}"}

    spec = ModelSpec(name=name, purpose=purpose, poly_budget=poly_budget, mode="local")
    report = validate.validate(mesh, profile=spec.purpose, spec=spec)

    previews: dict[str, str] = {}
    if render_previews:
        previews = render.render_views(mesh, out_dir / "previews", name=name,
                                       views=("iso", "front")).get("previews", {})

    html_path = None
    if make_viewer:
        try:
            html_path = str(viewer.build_viewer(glb, out_dir / f"{name}.html", spec=spec))
        except Exception:
            html_path = None

    size_mb = round(glb.stat().st_size / (1024 * 1024), 2)
    store.finish(job, str(glb), meta={"faces": info["faces"], "size_mb": size_mb})

    return {
        "ok": True,
        "job_id": job.id,
        "path": str(glb),
        "stl": str(stl),
        "viewer": html_path,
        "previews": previews,
        "planet": info,
        "size_mb": size_mb,
        "validation": {"ok": report.ok, "summary": report.summary()},
        "next": ("call make_scene_viewer to place several planets in one page"
                 if report.ok else "validation failed — inspect `previews` before shipping"),
    }


@mcp.tool
def make_scene_viewer(
    items: list[dict],
    title: str = "forge3d scene",
    out_html: str | None = None,
) -> dict:
    """Compose several models into ONE self-contained Three.js page.

    Each item: {"path": "/abs/model.glb", "name": "mars-prime",
                "position": [0,0,0], "scale": 1.0, "rotation": [0,0,0]}

    Every mesh is embedded as a data URI, so the page needs no server and no
    hosting. Watch the size: four textured planets is ~3.5 MB — fine to hand to
    someone, heavy as a website hero. For production, host the GLBs and use the
    `forge3d-viewer` custom element instead.
    """
    if not items:
        return {"ok": False, "error": "no items", "hint": "pass at least one {path: ...} entry"}
    dst = Path(out_html) if out_html else get_settings().ensure_workdir() / "viewer" / "scene.html"
    try:
        out = viewer.build_scene_viewer(items, dst, title=title)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not build scene viewer: {exc}"}
    size_mb = round(out.stat().st_size / (1024 * 1024), 2)
    result = {"ok": True, "html": str(out), "bodies": len(items), "size_mb": size_mb}
    if size_mb > 8:
        result["warning"] = (
            f"{size_mb} MB page — every mesh is embedded. For a website hero, host the "
            "GLBs and embed the forge3d-viewer custom element instead."
        )
    return result


@mcp.tool
def list_shapes() -> dict:
    """Catalogue of maths-defined shapes, with the equation and parameters for each.

    Call this before `create_shape`. Every entry states whether it is a closed
    solid (safe to print) or an open surface (visual only), and what each
    parameter does — so you can pick a family instead of writing vertex loops.
    """
    from .geometry.surfaces import list_shapes as _catalogue

    entries = _catalogue()
    return {
        "ok": True,
        "count": len(entries),
        "shapes": entries,
        "hint": "create_shape(shape='supershape', params={'m': 6, 'n1': 0.3})",
    }


@mcp.tool
def create_shape(
    shape: str = "supershape",
    params: dict | None = None,
    resolution: int = 96,
    name: str | None = None,
    purpose: str = "game",
    poly_budget: int | None = None,
    export_format: str = "glb",
    render_previews: bool = True,
    make_viewer: bool = True,
) -> dict:
    """Build a shape from a mathematical equation and export it to any mesh format.

    Pick a family with `list_shapes`, then pass the numbers. Everything is
    deterministic: the same shape and params give the same mesh every time.

    resolution controls detail (12-512). It is the one knob you usually need —
    raise it for a hero asset, lower it for a game prop or a phone scene.

    export_format: glb (default, web), stl (print), obj, ply, off, gltf, dae.
    GLB and STL are always written; export_format adds one more.

    Shapes marked `closed: false` (mobius, helicoid, catenoid) are open surfaces —
    visually correct, but not printable solids. `validate_model` will say so.
    """
    from .geometry.surfaces import build_shape

    try:
        mesh = build_shape(shape, params or {}, resolution=resolution)
    except KeyError as exc:
        return {"ok": False, "error": str(exc),
                "hint": "call list_shapes for the catalogue and their parameters"}
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"ok": False, "error": f"{shape} failed to build: {exc}"}

    label = name or f"{shape}"
    spec = ModelSpec(name=label, purpose=purpose, poly_budget=poly_budget, mode="local")
    out_dir = _models_dir() / spec.name / uuid.uuid4().hex[:12]
    out_dir.mkdir(parents=True, exist_ok=True)

    glb = out_dir / f"{spec.name}.glb"
    stl = out_dir / f"{spec.name}.stl"
    mesh.export(str(glb))
    mesh.export(str(stl))

    extras: dict[str, str] = {}
    fmt = (export_format or "glb").lstrip(".").lower()
    if fmt not in ("glb", "stl"):
        try:
            extras[fmt] = str(meshio.export(mesh, out_dir / f"{spec.name}.{fmt}"))
        except Exception as exc:  # noqa: BLE001
            extras["error"] = f"{fmt} export failed: {exc}"

    report = validate.validate(mesh, profile=spec.purpose, spec=spec)

    previews: dict[str, str] = {}
    if render_previews:
        previews = render.render_views(mesh, out_dir / "previews", name=spec.name,
                                       views=("iso", "front")).get("previews", {})

    html_path = None
    if make_viewer:
        try:
            html_path = str(viewer.build_viewer(glb, out_dir / f"{spec.name}.html", spec=spec))
        except Exception:
            html_path = None

    return {
        "ok": True,
        "shape": shape,
        "params": mesh.metadata.get("params", {}),
        "path": str(glb),
        "stl": str(stl),
        "extra_formats": extras,
        "viewer": html_path,
        "previews": previews,
        "card": {"triangles": int(len(mesh.faces)),
                 "extents": [round(float(e), 3) for e in mesh.extents],
                 "watertight": bool(mesh.is_watertight)},
        "validation": {"ok": report.ok, "summary": report.summary(),
                       "failed": [c.id for c in report.checks if not c.ok]},
        "next": ("ship it" if report.ok
                 else "the shape is fine but not a closed solid — fine for display, "
                      "not for printing. Try a different family, or repair_model."),
    }


@mcp.tool
def list_providers(check: bool = False) -> dict:
    """List text-to-3D providers: what is configured here, and what is verified.

    Call this before choosing `backend="hosted"`. `available` means credentials
    exist on this machine; `verified` means the adapter has been exercised against
    a live account by the maintainer. Most commercial adapters are NOT verified —
    pass check=true to make a real authenticated request and settle it.
    """
    from .providers import registry as provider_registry

    providers = provider_registry.list_providers()
    result = {
        "ok": True,
        "providers": providers,
        "configured": [p["name"] for p in providers if p["available"]],
        "unverified": [p["name"] for p in providers if not p["verified"]],
        "hint": "set FORGE3D_PROVIDER + the provider's key env var, then "
                "backend='hosted'. Or point FORGE3D_HOSTED_URL at your own worker.",
    }
    if check:
        result["checks"] = provider_registry.check_all()
    return result


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
    """Run the MCP server.

    stdio (default) is for a local agent you already trust. HTTP is for remote
    agents and app integration, and it is deliberately harder to expose: it binds
    loopback by default, refuses a public interface without a token, and forces
    subprocess isolation for model code.
    """
    import os
    import sys

    from .http_server import parse_http_args, serve

    argv = sys.argv[1:]
    if any(a == "--http" or a.startswith("--http=") for a in argv):
        cfg = parse_http_args(argv)
        # An HTTP caller is not necessarily trusted: run model code in a killable
        # child unless the operator explicitly chose otherwise.
        os.environ.setdefault("FORGE3D_TRANSPORT", "http")
        if os.environ.get("FORGE3D_EXEC_MODE") in (None, "", "auto"):
            os.environ["FORGE3D_EXEC_MODE"] = "subprocess"
        get_settings(refresh=True)
        serve(mcp, cfg)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
