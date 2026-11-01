# Parametric surfaces

Every shape here is defined by an equation, not by hand-placed vertices. An agent
picks a family by name, passes a few numbers, and gets a mesh — deterministic,
resolution-controlled, and exportable to any supported format.

```python
create_shape(shape="supershape", params={"m": 6, "n1": 0.3}, resolution=96)
create_shape(shape="involute_gear", params={"teeth": 24}, export_format="stl")
```

Call `list_shapes` first — it returns this catalogue with the equations, defaults
and notes, so the agent does not have to guess what a parameter does.

## Solids (closed manifolds)

These are watertight with consistent winding, so they pass the `print` profile and
can ship as game assets.

### supershape — Gielis superformula
One equation that covers flowers, stars, gears and organic blobs.

$$
r(\theta) = \left( \left|\frac{\cos(m\theta/4)}{a}\right|^{n_2} + \left|\frac{\sin(m\theta/4)}{b}\right|^{n_3} \right)^{-1/n_1}
$$

$$
r(\theta,\varphi) = r(\theta)\cdot r(\varphi)
$$

| param | default | effect |
|---|---|---|
| `m` | 6 | rotational symmetry — teeth, petals, lobes |
| `n1` | 0.3 | overall sharpness; small → spiky, large → round |
| `n2`, `n3` | 0.6 | balance between the two axes |

`m=2, n1=1` is a sphere. `m=5, n1=0.3` is a starfish. `m=8, n1=0.2, n2=n3=0.8` is
a gear-like disc.

### spherical_harmonic — seeded blobs
$$
r(\theta,\varphi) = 1 + \sum_i a_i \sin(m_i\theta + \varphi_i)\sin^{m_i/2}\varphi
$$

Change `seed` for a different blob, `terms` for complexity. This is the classic
"mathematical potato" and it is genuinely useful for organic props.

### superellipsoid — boxes, pillows and lenses
$$
\left|\frac{x}{a}\right|^{2/e_1} + \left|\frac{y}{b}\right|^{2/e_1} + \left|\frac{z}{c}\right|^{2/e_1} = 1
$$

| `e1`, `e2` | result |
|---|---|
| 0.2 | boxy, near-cubic |
| 0.5 | rounded box (default) |
| 1.0 | ellipsoid |
| 2.0 | lens / biconvex |

### torus_knot — (p, q) knot
$$
x = (R + r\cos q\varphi)\cos p\varphi,\quad
y = (R + r\cos q\varphi)\sin p\varphi,\quad
z = r\sin q\varphi
$$

`p, q` coprime gives a true knot: (2,3) trefoil, (3,4), (5,2). The tube is swept
with parallel-transport frames, so there is no Frenet twist artefact.

### lissajous — three frequencies
$$
x = \sin(at+\delta),\quad y = \sin(bt),\quad z = \sin(ct+\delta/2)
$$

Coprime triples give closed knots. `a=3, b=4, c=5` is the default.

### spring — helix
$$
x = R\cos t,\quad y = R\sin t,\quad z = \text{pitch}\cdot t
$$

`turns`, `radius`, `pitch`, `wire` (tube radius). A real printable spring.

### seashell — logarithmic spiral
$$
\rho(t) = \rho_0 e^{bt}
$$

swept as a tube with constant wire radius. `growth` is the constant that makes a
shell look like a shell rather than a spiral.

### involute_gear — real meshing teeth
$$
x = r(\cos t + t\sin t),\quad y = r(\sin t - t\cos t)
$$

The involute of a circle — the profile that actually transmits torque. Straight
radial teeth look plausible and do not work.

| param | meaning |
|---|---|
| `teeth` | tooth count |
| `module` | pitch diameter ÷ teeth |
| `thickness` | extrusion depth |
| `pressure_angle` | degrees, 20 is standard |

### twisted_prism
A regular n-gon extruded with a twist about its axis. Columns, towers, flutes.

### lathe — solid of revolution
Spin a 2D profile `[(radius, height), ...]` around the vertical axis. This is the
most reusable of the set: give it a silhouette and you get a vase, a bolt, a bowl,
a chess piece. Zero-radius endpoints are capped with a single apex vertex, which
is what keeps the result watertight.

### bumpy_sphere
$$
r(\theta,\varphi) = R\left(1 + A\sin(f_u\theta)\sin(f_v\varphi)\right)
$$

The simplest useful deformation, and a good pipeline smoke test.

## Open surfaces

Correct geometry with a boundary. Visually right, not printable as a solid —
`validate_model` says so rather than pretending otherwise.

### mobius — one side, one edge
$$
x = (R + v\cos(u/2))\cos u,\quad y = (R + v\cos(u/2))\sin u,\quad z = v\sin(u/2)
$$

### helicoid — screw surface
$$
x = v\cos u,\quad y = v\sin u,\quad z = cu
$$

### catenoid — the only minimal surface of revolution
$$
x = c\cosh(v/c)\cos u,\quad y = c\cosh(v/c)\sin u,\quad z = v
$$

## Fractal

### menger_sponge
Recursive removal of the centre cross: 20 cubes per level. Returned as a
**multi-body** mesh (20^level separate closed boxes), not one manifold. That is
the honest description of a fractal, and `validate_model` reports the body count.

## Formats

`create_shape` always writes GLB (web) and STL (print), and `export_format` adds
one more:

| format | use |
|---|---|
| `glb` | web, game, AR — embedded materials |
| `stl` | 3D printing |
| `obj` | interchange, DCC import |
| `ply` | scan/point-cloud adjacency |
| `gltf` | JSON glTF, with sidecar buffers written alongside |
| `dae` | needs `pip install pycollada` |
| `step` | not a mesh — use the `cadquery` backend for B-rep |

## Resolution

`resolution` (12–512) drives both grid directions; higher is smoother and heavier.
Faces scale roughly with resolution², so:

| resolution | supershape faces | feel |
|---|---|---|
| 32 | ~600 | game prop |
| 96 | ~6k | default, good for web |
| 256 | ~44k | hero asset |
| 512 | ~175k | only if you need it |

The resolution is clamped rather than trusted: `resolution=5000` will not try to
allocate a 5000×2500 grid.

## Why these are not in `procedural.py`

They were, in effect — every agent that wanted a knot or a gear wrote the same
vertex loops, and most versions leaked at the caps. Implementing each family once,
with a test that asserts watertightness, is the difference between a demo and a
library.
