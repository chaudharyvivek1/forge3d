---
name: forge3d
description: Create, validate and ship 3D models from an AI agent. Use when the user asks for a 3D model, mesh, printable part, game asset, GLB/STL/STEP export, a Three.js preview, or to fix and inspect an existing 3D file. Routes to Blender, OpenSCAD, CadQuery, trimesh procedural code or a hosted text-to-3D service, then validates against the delivery target and produces a shareable viewer page.
---

# forge3d

You are working with a 3D modelling toolchain exposed over MCP. The tools do the
geometry; **you** are responsible for the brief, the review loop, and telling the
truth about what came out.

## The one rule that matters

**Never paste geometry into context.** No vertex lists, no base64, no file dumps.
Pass paths. Describe models with `model_card` (about 300 tokens). If you find
yourself about to print mesh data, you have taken a wrong turn.

## Workflow

### 1. Brief — `plan_model`
Never call `create_model` from a bare prompt. Call `plan_model` first and read
`missing`. If `purpose` or `dimensions` is missing, **ask the user** — do not
invent a size. A model with no scale is not a deliverable.

Decide, and record in the spec:
- `purpose`: `print` | `web` | `game` | `cad` — this changes what "correct" means
- `dimensions` in real units (`mm` for print and CAD, usually)
- `poly_budget` for web and game
- whether the user needs an editable source (code backends) or just a mesh

### 2. Build — `create_model`
Let `backend="auto"` route unless you have a reason. Reasons to override:
- exact dimensions, hard surface, CSG → `openscad`
- organic, bevels, materials, animation → `blender`
- STEP file, manufacturing → `cadquery`
- textured props fast, no scale requirement → `hosted`
- nothing installed / offline / must be editable → `procedural`

Read the result:
- `ok: false` → read `log`, fix the **source**, retry the same spec. Do not
  change the spec to make an error go away.
- `validation.ok: false` → go to step 4. Do not report success.

### 3. Look at it — `render_model`
A model you have not seen is a guess. Call `render_model` (or rely on the
previews `create_model` returns) and actually inspect the images. Look for:
floating parts, wrong orientation, collapsed booleans, detail that vanished.

### 4. Check — `validate_model`
Profiles and what they enforce:

| profile | errors (blockers) | warnings (judgement) |
|---|---|---|
| `print` | watertight, consistent winding, positive volume | single body, face budget |
| `web` | geometry present, non-zero dimensions | UVs, draw-call count, face budget |
| `game` | geometry present | LOD headroom, body count |
| `cad` | watertight, winding, volume | single body |

Every failure carries a `hint` naming the fix. Use it.

### 5. Fix — `repair_model`
`fill_holes`, `fix_normals`, `decimate_to`. It never overwrites the input unless
you pass the same path. Report the `actions` it took — a user deserves to know
their mesh was decimated from 400k to 60k triangles.

Then re-run `validate_model` or `review_model`. **Fix and re-check; never
re-check and hope.**

### 6. Ship — `make_viewer` + `convert_model`
- print → `.stl` (or `.3mf`), 1:1 scale, mm
- web / game → `.glb`, textures embedded
- manufacturing → `.step` (only from the `cadquery` backend)
- `make_viewer` gives a single HTML file that works offline — the fastest way to
  let a non-technical user see the result

### 7. Hand off — `create_handoff`
When another agent, or a human, picks this up next, write the packet. Include
`owners` and `open_questions`. It costs one call and prevents duplicated work.

## Checklist before you say "done"

- [ ] purpose and real dimensions were confirmed with the user, not assumed
- [ ] validation passes for the target profile (or you have told the user exactly
      which warnings remain and why they are acceptable)
- [ ] you have looked at at least one preview render
- [ ] the export format matches the delivery target
- [ ] triangle count is inside the budget for web/game
- [ ] you told the user where the files are, in plain language
- [ ] licensing/attribution caveat mentioned if a hosted service or reference
      image was involved

## Anti-patterns

| Don't | Do |
|---|---|
| `create_model` straight from a one-line prompt | `plan_model`, fill gaps, then build |
| claim success on `ok: true` while `validation.ok` is false | report the failures and their hints |
| regenerate the whole model to fix one hole | `repair_model` |
| paste a GLB or vertex array into context | `model_card`, then pass the path |
| guess the backend | `list_backends` — availability is ground truth |
| eyeball millimetres | put dimensions in the spec, use `scale_to_spec` |
| describe a mesh you never rendered | render, then look |

## Reference files

- `references/backends.md` — when each engine wins, with code shapes
- `references/checklists.md` — full checklists by delivery target
- `references/threejs.md` — viewer embedding, performance, app integration
- `references/prompts.md` — how to write briefs that produce usable geometry
