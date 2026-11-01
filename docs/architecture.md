# Architecture

## Why it is layered like this

An AI agent doing 3D work fails in four predictable places, and each layer exists
to catch one:

1. **It builds the wrong thing.** No scale, no purpose, no acceptance criteria.
   → `spec.py`
2. **It is locked to one engine**, so it cannot express the shape it described.
   → `backends/`
3. **It cannot see what it made**, so it cannot iterate.
   → `render.py`, `validate.py`
4. **It floods its own context** with geometry and loses the thread.
   → `context.py`, `handoff.py`

Everything else is plumbing around those four.

```
                    ┌──────────────┐
   agent / user ───►│  server.py   │  MCP: 13 tools, JSON in, JSON out
                    │  cli.py      │  same capabilities, no agent
                    └──────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ┌─────────┐      ┌────────────┐     ┌───────────┐
   │ spec.py │      │ backends/  │     │  jobs.py  │
   │  brief  │      │ 5 engines  │     │ durable   │
   └────┬────┘      └─────┬──────┘     └───────────┘
        │                 │
        │                 ▼
        │          ┌─────────────┐
        │          │  meshio.py  │  one geometry currency: Trimesh / Scene
        │          └──────┬──────┘
        ▼                 ▼
   ┌──────────┐   ┌──────────────┐   ┌──────────┐   ┌──────────┐
   │validate  │   │   repair     │   │  render  │   │  viewer  │
   │ profiles │   │ merge/fill   │   │  PNG     │   │  HTML    │
   └──────────┘   └──────────────┘   └──────────┘   └──────────┘
                           │
                    ┌──────┴───────┐
                    │  context.py  │  model cards — 300 tokens, not 3 million
                    │  handoff.py  │  multi-agent / multi-human packets
                    └──────────────┘
```

## Key decisions

**Paths, never payloads.** Every public function takes and returns file paths or
small dicts. No tool returns vertex data. This is the difference between an agent
that can do twenty iterations in one context window and one that can do two.

**Geometry currency is `trimesh.Trimesh` or `trimesh.Scene`.** No backend-specific
object leaks past `backends/`. That is what makes the engines interchangeable and
`validate`/`render`/`viewer` engine-agnostic.

**Validation is profile-based, and every check has a severity.** `error` blocks
delivery; `warn` is a judgement call. A game asset and a printable part have
genuinely different hard requirements, so one global "is it good" check would be
either useless or wrong.

**Every failure returns a `hint`.** An agent that receives `"repair_model with
fill_holes=true"` self-corrects. An agent that receives a traceback gives up or
hallucinates a fix.

**Rendering is CPU-only.** `matplotlib` + a painter's-algorithm poly collection,
no GPU, no OpenGL, no `pyglet`. It works in a headless container, which is where
agents actually run. Quality is "enough to spot a floating leg", not "beauty
shot" — the Three.js viewer is for beauty.

**Routing is a function, not a prompt.** `resolve_auto()` is a plain Python
function over the spec, so it is unit-testable and reviewable. Prompt-based
routing is untestable and drifts.

**Job records, because generation is slow.** Hosted generation can take minutes.
A job record with `queued/running/succeeded/failed` plus a log means a timeout
does not lose the work, and an agent can poll instead of blocking.

## Data flow for one model

```
ModelSpec (name, prompt, purpose, units, dimensions, budget)
   │
   ├─ resolve_auto() ──► backend name
   │
   ├─ backend.generate(spec, out_dir, source) ──► GLB/STL/STEP on disk
   │
   ├─ meshio.load() ──► Trimesh
   ├─ validate.scale_to_spec() (optional, uniform)
   ├─ validate.validate(profile=purpose) ──► Report{checks[], ok}
   ├─ render.render_views() ──► 4 PNGs
   ├─ viewer.build_viewer() ──► single-file HTML
   └─ context.model_card() ──► ~300-token dict
```

## Extension points

**A new engine** — subclass `backends.base.Backend`, set `name`, `kind`,
`requires`, `capabilities`, implement `available()` and `generate()`, add it to
`registry.ALL`. Nothing else changes; validation, rendering and the viewer work
immediately because they only know about `Trimesh`.

**A new delivery profile** — add checks in `validate.validate()` under a new
profile name and add it to `PROFILES`. Add a matching checklist in
`handoff.CHECKLISTS`.

**A new tool** — add a `@mcp.tool` function in `server.py` returning a dict, and
a matching subcommand in `cli.py`. Keep the return under a few hundred tokens.

## Testing strategy

`tests/` runs against the procedural backend only, because it needs no external
binary — so CI is honest on any machine. Tests that would require OpenSCAD,
Blender or CadQuery skip themselves when the binary is absent rather than failing,
and `list_backends` availability is asserted to match reality. See
`tests/test_backends.py`.
