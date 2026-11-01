# Writing briefs that produce usable geometry

A 3D generator cannot resolve ambiguity the way an image model can — it has to
commit to numbers. The gap between "a cool robot" and a usable asset is entirely
in the brief.

## The four fields that decide everything

| Field | Why it decides the output |
|---|---|
| **purpose** | `print` needs watertight and 1:1; `web` needs UVs and a budget; `game` needs a pivot convention and LODs; `cad` needs B-rep |
| **dimensions** | without real size, nothing downstream is checkable — and unit mix-ups are the #1 silent bug |
| **silhouette** | what the shape reads as from 3 m away; name it explicitly |
| **structure** | one solid piece, or a set of parts that must fit together |

## Weak vs strong

**Weak**
> make me a robot

**Strong**
> A desk-toy robot, 120 mm tall, standing on a 60×60 mm base. Blocky retro
> silhouette: rectangular torso, cylindrical head, two arm cylinders with
> 3-finger claws. Purpose: print (FDM, 0.4 mm nozzle). Must be one watertight
> body, no supports needed — keep overhangs under 45°. Poly budget 40k.

The second version can be *validated*. The first can only be admired.

## Vocabulary that actually changes geometry

Prefer measurable words over taste words:

| Vague | Instead |
|---|---|
| "modern" | "flat planes, 3 mm bevels, no ornament" |
| "sleek" | "single continuous surface, radii ≥ 20 mm" |
| "chunky" | "wall thickness 4 mm, edges 6 mm radius" |
| "detailed" | "panel lines every 25 mm, 1 mm deep" |
| "low poly" | "under 15,000 triangles, flat-shaded" |
| "realistic" | "matches photo reference, PBR, 2k textures" |

## Prompt shapes per backend

**procedural / openscad / cadquery** — do not write prose, write parameters.
The brief becomes a comment block, the shape becomes numbers:

```
SEAT_W = 420   # mm, matches the 450 mm overall width target
BACK_H = 400   # mm, 8 deg recline
LEG_W  = 34    # mm, 2 mm chamfer
```

**blender** — prose plus modifiers. Say which modifier and its value; do not
say "make it smooth" and hope.

**hosted text-to-3D** — these models respond to *style and silhouette*, not
dimensions. Lead with the subject and the style, keep it under ~60 words, and
add a negative-style clause if the service supports one:

> "minimalist ceramic teapot, matte white glaze, single smooth body, wide
> cylindrical silhouette, studio product render, no ornamentation"

Then fix scale yourself with `scale_to_spec`, and expect to `repair_model` before
printing anything that came from a hosted model.

## Questions worth asking the user

Ask these instead of guessing. They are cheap; a wrong model is not.

1. What is this for — printing, a website, a game, or CAD/manufacturing?
2. What size in millimetres (or the real object it must fit)?
3. Does it need to be one solid piece, or are separate parts fine?
4. Is an editable source needed, or just the mesh?
5. Is there a reference image or an existing model to match?
6. Will this be sold or published? (licensing follows from this)

If the user cannot answer 1 or 2, propose a default and say so explicitly:
"assuming 1:10 scale display model, 120 mm tall — correct me if not."

## Multi-part assemblies

Model the assembly as a list of named parts, not one blob. Then each part can be
validated, oriented and exported for its own process (a printed bracket plus a
machined pin, say). `build()` may return a list; forge3d keeps them as separate
geometries in a scene, and `inspect_model` reports the body count.
