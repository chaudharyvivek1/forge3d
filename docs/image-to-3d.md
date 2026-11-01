# Image-to-3D

There are two completely different things people mean by "image to 3D", and
conflating them is how projects end up with a demo that cannot ship.

**Reconstruction** — infer an object's full 3D form from one photo, including the
side you cannot see. This needs a neural model (TRELLIS, Stable Fast 3D,
Hunyuan3D, TripoSR), a GPU, and several gigabytes of weights. forge3d does not
ship one. Point the `hosted` backend at a provider that does.

**Derivation** — compute real geometry from the pixels you have: heights from
luminance, profiles from alpha. No GPU, no weights, deterministic, and the output
is a watertight solid you can print. That is the `image` backend.

```
forge3d create --name plaque --purpose print --backend image \
    --source photo.jpg --dims x=120,y=90,z=12
```

## Modes

| Mode | What it does | Good for | Output |
|---|---|---|---|
| `relief` | luminance → height, on a solid base | lithophanes, plaques, coins, terrain from a height map | watertight solid, flat back |
| `silhouette` | alpha (or luminance) → extruded profile | logos, signs, gaskets, cookie cutters, tags | watertight solid, holes preserved |
| `auto` | transparent background → silhouette, otherwise relief | the default; right most of the time | either |

### relief

Every pixel becomes a vertex at a height proportional to its brightness, plus a
base slab so the result is a printable solid rather than a single-sided shell.

```python
create_model(
    backend="image", source="portrait.jpg", purpose="print",
    dimensions={"x": 100, "y": 75, "z": 8},   # 8mm total thickness
    # backend options:
    #   height=4.0     mm from base to brightest point
    #   base=1.2       mm solid backing
    #   invert=True    dark = tall (lithophanes want this)
    #   smooth=1       gaussian sigma in cells; 0 keeps every pixel edge
    #   grid=192       sampling resolution, 32..512
)
```

Brightness mapping: white is high, black is low, unless `invert=True`. A lithophane
— the thing you hold up to a lamp — wants `invert=True` with a thin base, because
thin plastic transmits more light, so dark regions should be *thin*.

### silhouette

The mask is extruded to a thickness and capped top and bottom. Only *exposed*
faces are emitted, so the result is a clean manifold rather than a pile of
welded boxes, and a hole in the mask (the counter of an "O", a gasket cut-out)
survives as a real hole.

```python
create_model(
    backend="image", source="logo.png", purpose="print",
    dimensions={"x": 80, "y": 80, "z": 4},
    #   thickness=4.0  mm
    #   threshold=0.5  alpha (or luminance) cut
)
```

This is exact enough to test arithmetically: a disc with a hole of inner radius 14
and outer radius 40 keeps $$1 - (14/40)^2 = 0.8775$$ of the solid disc's volume,
and the test asserts that ratio to within 4%.

## Resolution and budgets

`grid` is the sampling resolution. A relief costs exactly
$$4(g-1)^2 + 8(g-1)$$ triangles, so:

| grid | triangles | typical build |
|---|---|---|
| 64 | ~16k | instant |
| 192 (default) | ~146k | ~1 s |
| 512 (max) | ~1.05M | tens of seconds, hundreds of MB |

If the spec has a `poly_budget`, the grid is reduced automatically to fit it —
before generation, not by decimating afterwards. A 60k-triangle game asset asked
for at `grid=512` gets a grid that actually fits, and `meta.requested_grid` versus
`meta.grid` records that it happened.

## What it will not do

- Reconstruct the back of an object. A relief has a flat back by construction.
- Texture anything. There is no UV map and no material.
- Handle a photo with a busy background well in `silhouette` mode — thresholding
  a cluttered scene gives a cluttered extrusion. Cut the subject out first, or
  use `relief`.
- Replace a hosted model for organic full-body shapes. Use `backend="hosted"`.

`forge3d doctor` and `list_backends` report exactly this, so an agent that asks
for a character from a photo is told to route to a provider rather than handed a
disappointing plaque.

## Routing

`backend="auto"` sends a spec to `image` when `references[0]` is a **local file**.
A URL reference does not route here — that goes to `hosted` if configured, because
a URL usually means "use the model", not "extrude these pixels".

```python
plan_model(prompt="logo sign", purpose="print", references=["/path/logo.png"])
# -> recommended_backend: image
```

## Checking the result

Image-derived meshes are solids, so they pass the `print` profile cleanly:

```
forge3d create --name plaque --purpose print --backend image --source photo.jpg
forge3d validate out/plaque.glb --profile print     # watertight, positive volume
forge3d render   out/plaque.glb                     # look at it
```

The usual failure is not geometry but *content*: a photo with too little contrast
gives a flat plaque, and a threshold too high gives an empty mask (reported as
"the image produced no geometry" with the suggestion to lower `threshold`).
