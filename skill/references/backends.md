# Backends — choosing an engine

`forge3d backends` is ground truth for availability. Everything below is about
*fit*, not availability.

## The decision, in one table

| You are making | Engine | Because |
|---|---|---|
| a bracket, jig, enclosure, mount | `openscad` | exact CSG, boolean-clean, parameterised |
| furniture, anything with repeated structure | `procedural` | numbers at the top, rebuildable, diffable |
| a character, creature, organically shaped prop | `hosted` or `blender` | mesh-from-prompt or subdivision + sculpt |
| a product visual with nice bevels and materials | `blender` | modifiers, PBR, render engine |
| a part that must go to a machine shop | `cadquery` | real B-rep, STEP export, fillets that survive |
| a game asset under a triangle budget | `blender` → `repair_model` | decimation + LOD chain |
| something in a sandbox with nothing installed | `procedural` | only needs trimesh |

## procedural — the agent writes Python

Source must define `build()`. Preloaded namespace: `trimesh`, `np`, `math`,
`spec`, `box(x,y,z)`, `cyl(r,h)`.

```python
import trimesh

def build():
    seat = trimesh.creation.box(extents=[420, 400, 45])
    seat.apply_translation([0, 0, 430])
    legs = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            leg = trimesh.creation.box(extents=[34, 34, 400])
            leg.apply_translation([sx * 175, sy * 165, 200])
            legs.append(leg)
    return trimesh.util.concatenate([seat, *legs])
```

Returns `Trimesh`, `Scene`, or a list of either. Exports GLB + STL.

**Win:** deterministic, editable, no external binary, exact numbers.
**Lose:** organic forms. `trimesh` primitives are boxes, cylinders, spheres,
capsules, extrusions and lathes — if the shape cannot be expressed as those plus
booleans, you are on the wrong backend.

Booleans need `pip install manifold3d`. Without it, `trimesh.boolean` raises —
so concatenate, or install it.

## image — geometry derived from pixels

Two modes, both deterministic and watertight:

```python
# relief: brightness becomes height (lithophane, plaque, height map)
create_model(backend="image", source="portrait.jpg", purpose="print",
             dimensions={"x": 100, "y": 75, "z": 8})

# silhouette: alpha becomes an extruded profile, holes preserved (logo, gasket)
create_model(backend="image", source="logo.png", purpose="print",
             dimensions={"x": 80, "y": 80, "z": 4}, thickness=4.0)
```

**Win:** needs nothing but PIL; exact arithmetic; the only local route from an
image to a printable solid.
**Lose:** it derives, it does not reconstruct — no hidden geometry, no textures,
no organic characters. For those, route to `hosted`.

Budget note: relief cost is `4(g-1)^2 + 8(g-1)` triangles at grid `g`. Pass
`poly_budget` in the spec and the grid is reduced to fit *before* generating.

## openscad — declarative CSG

Pass the `.scad` body; `$fn` is added for you unless present.

```openscad
difference() {
  cube([120, 60, 40], center = true);
  translate([0, 0, -25]) cylinder(h = 60, d = 32);
  for (x = [-40, 40]) translate([x, 0, 0]) cylinder(h = 60, d = 4.5);
}
```

**Win:** exact geometry, trivially parametric, best error messages of any engine.
**Lose:** no organic shapes, no textures, slow on heavy CSG trees.

## blender — headless bpy

Script receives the export path as `sys.argv[-1]`. If you do not export, forge3d
appends a GLB export for you.

```python
import bpy

bpy.ops.mesh.primitive_gear(number_of_teeth=24, radius=2.0, depth=0.4)
obj = bpy.context.object
mod = obj.modifiers.new("bevel", 'BEVEL')
mod.width = 0.02
mod.segments = 3
```

**Win:** anything with a surface quality requirement; materials; rigging.
**Lose:** not deterministic across versions; slow startup (~2–5 s); dimension
precision is awkward compared with CAD.

## cadquery — B-rep

Assign `result`.

```python
import cadquery as cq

result = (
    cq.Workplane("XY")
    .box(120, 60, 40)
    .faces(">Z").workplane().hole(32)
    .edges("|Z").fillet(3)
)
```

Exports STEP **and** STL. STEP is the reason to use this backend.

## hosted — text-to-3D / image-to-3D

Speaks one documented contract (see `backends/hosted.py`):

```
POST {FORGE3D_HOSTED_URL}/generate  ->  {job_id}  or  {asset_url}
GET  {FORGE3D_HOSTED_URL}/jobs/{id} ->  {status, asset_url, error}
```

Provider APIs differ enough that a "universal client" would be a lie. Write a
~40-line worker in front of whichever service you use, normalise it to the
contract above, and forge3d stays provider-agnostic.

**Win:** the only route to textured, organically shaped output.
**Lose:** no exact dimensions, rarely watertight, non-repeatable, per-call cost,
and the provider's licence governs the output.

Always run `validate_model` afterwards, and always `scale_to_spec` if the model
needs real-world size.

## auto routing

```
source looks like bpy        -> blender
source looks like cadquery   -> cadquery
source looks like .scad      -> openscad
purpose == cad               -> cadquery (if installed)
purpose == print             -> openscad (if installed) else procedural
purpose == game              -> blender (if installed) else procedural
purpose == web               -> hosted (if configured) else procedural
spec has references          -> hosted (if configured)
otherwise                    -> procedural
```

Edit `src/forge3d/backends/registry.py::resolve_auto` — it is a plain function,
not a prompt, so it is unit-testable.
