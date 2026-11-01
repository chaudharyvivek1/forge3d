# Procedural geometry generators

A *shape family* implemented once, with parameters and tests, instead of being
re-derived in every agent's model source. Today: planets. Next: terrain tiles,
rocks, and architectural massing — same contract.

```python
from forge3d.geometry.planets import PlanetParams, generate_planet

mesh, info = generate_planet(PlanetParams(radius=100, seed=7, canyon=True))
```

Or through the agent surface:

```python
create_planet(radius=100, seed=7, canyon=True, subdivisions=5, texture_resolution=512)
```

## Why this exists

Writing `trimesh` primitives is fine for a bracket and hopeless for a planet.
Planets need four things that are individually easy and collectively fiddly:

| Problem | The usual wrong answer | What this does |
|---|---|---|
| Seamless surface | noise sampled in UV space → a visible seam down the back | noise sampled in **3D on the unit sphere**, so there is no seam to hide |
| Craters that read as geology | one more noise octave → dimples | analytic **bowl + raised rim** from seeded impact sites |
| Textures that line up with the geometry | paint a map, hope it matches | bake the maps **from the same fields**, on **spherical UVs** |
| Reproducibility | "looks different every run" | everything derives from `seed`; same params, same bytes |

## Parameters

| Parameter | Effect |
|---|---|
| `radius` | world units |
| `seed` | any int; determines terrain, crater placement, canyon orientation |
| `subdivisions` | icosphere level: 4 ≈ 5k faces, 5 ≈ 20k, 6 ≈ 82k |
| `terrain_amplitude` | relief as a fraction of radius (0.035 default ≈ 3.5%) |
| `roughness` | noise frequency; higher is busier |
| `crater_density` | 0..1 — number of impact sites |
| `crater_size_range` | min/max crater radius as a fraction of planet radius |
| `canyon` / `canyon_depth` / `canyon_width` | a great-circle rift |
| `palette` | `"mars"`, `"moon"`, or `{low, mid, high}` RGB triples |
| `texture_resolution` | bake size; `0` disables baking (vertex colours only) |

## Baked textures

`texture_resolution > 0` writes three PNGs and embeds them in the GLB:

- **albedo** — palette ramp by elevation, plus dust grain in the lows
- **metallic_roughness** — glTF-packed: `G` = roughness, `B` = metallic (0 for rock)
- **normal** — derived from the height gradient, tangent space

Packing metallic and roughness into one map is not cosmetic. A standalone
"roughness" image is silently dropped by glTF exporters, which is how a PBR set
looks correct on disk and flat in the viewer. That was a real bug here.

This is an honest partial answer to "no PBR pipeline": it is procedural and
baked, with no occlusion pass and no hand-authored detail normal. It is not
Blender's shader graph. It does mean a generated planet is not a flat-coloured
ball.

## The Mars scene, end to end

The four-planet hero scene from the reference image, in two tool calls per
planet plus one compose step:

```python
planets = [
    ("mars-prime",  dict(radius=55, seed=11, canyon=True, subdivisions=5, texture_resolution=512), [0, 0, 0]),
    ("mars-small",  dict(radius=16, seed=22, subdivisions=4, texture_resolution=256), [95, 40, -30]),
    ("mars-mid",    dict(radius=22, seed=33, subdivisions=4, texture_resolution=256), [85, -30, -40]),
    ("mars-canyon", dict(radius=28, seed=44, canyon=True, subdivisions=5, texture_resolution=512), [105, -60, -80]),
]

items = []
for name, params, position in planets:
    made = create_planet(name=name, **params)
    items.append({"path": made["path"], "name": name, "position": position})

make_scene_viewer(items=items, title="Four-Planet Mars Scene")
```

That produces one self-contained HTML page: four planets at different scales,
one shared sun, a 9,000-point starfield, orbit controls and a snapshot button.

Measured on the reference scene: **51k triangles, ~17 s to build, 3.5 MB page.**

## Size, and what to do about it

3.5 MB is fine to hand to a designer and too heavy for a website hero. Two
levers, in order of effect:

1. **`texture_resolution`** — 512 → 256 roughly quarters the embedded bytes. The
   dominant cost is textures, not geometry.
2. **Host the GLBs.** `make_scene_viewer` embeds everything so the page is
   portable. For production, host the `.glb` files and use the
   `forge3d-viewer` custom element, which takes URLs and streams them.

`subdivisions` matters less than people expect: 20k → 82k faces is ~700 KB, while
one 512² albedo map is ~1 MB.

## What it does not do

- **Gas giants.** Banded atmosphere is a shader problem, not a mesh problem.
- **Rings, atmospheres, moons in orbit.** Add them as separate meshes in the
  scene; nothing here generates them.
- **Real planetary science.** Crater size distribution is plausible, not
  measured. This is art direction with parameters, not a simulation.
- **Animation.** Rotation is a viewer concern (the scene page auto-rotates); the
  GLB has no animation track.
