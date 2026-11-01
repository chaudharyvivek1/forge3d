# Formats, scale and licensing

## Format cheat-sheet

| Target | Ship | Never ship | Why |
|---|---|---|---|
| FDM / resin printing | `.stl` (binary) or `.3mf` | `.glb` | slicers want a closed surface and nothing else; `.3mf` carries units and multiple parts |
| Web / AR | `.glb` | `.fbx` | one file, embedded textures, universally supported |
| Game engine | `.glb` or `.fbx` | `.stl` | needs materials, UVs, skinning |
| Manufacturing / CAD | `.step` | any mesh | B-rep survives; a mesh loses analytic surfaces |
| Archival | `.glb` + original source | a lone `.stl` | the source is the only editable artifact |
| Blender round-trip | `.glb` | `.fbx` unless required | deterministic, PBR-preserving |

forge3d writes `.stl .obj .ply .off .glb .gltf .dae` and reads those plus `.3mf`,
`.usd`, `.usdz`, `.fbx` (via trimesh, where the optional dependency is present).
`.step` comes only from the `cadquery` backend.

## Scale — the silent bug

Unit mix-ups are the most common failure in agent-generated 3D, and they are
invisible in a viewport because a viewer auto-frames.

Anchors worth memorising:

| Object | Real size |
|---|---|
| phone | ~75 × 150 × 8 mm |
| mug | ~85 mm diameter, 95 mm tall |
| side chair | ~450 × 480 × 850 mm |
| door | ~800 × 2040 mm |
| M4 bolt hole | 4.5 mm clearance |
| print bed (common) | 220 × 220 × 250 mm |

Conventions:
- **STL has no units.** 1 unit = 1 mm by universal slicer convention.
- **glTF has no units either**, but the ecosystem assumes metres. A 450 mm chair
  is `0.45` in a glTF intended for a three.js scene that treats units as metres.
- **Unity / Unreal** treat 1 unit as 1 m and 1 cm respectively.
- **Blender** is unit-agnostic internally; set `scene.unit_settings` explicitly.

`validate_model` flags implausible extents and `dimensions_match_spec` drift.
`scale_to_spec` does a uniform scale to hit the dominant axis — it will not fix a
non-uniform unit error, and it should not: a model built at the wrong aspect ratio
is a modelling bug, not a scaling bug.

## Licensing — read this before shipping anything

forge3d's MIT licence covers **the source code**. It does not cover generated
assets, and it cannot: output rights are set by your inputs and your tools.

**Local engines** (procedural, OpenSCAD, Blender, CadQuery) produce geometry from
your own code and your own parameters. The output is normally yours to license as
you wish — but:

- **Blender** is GPL-2.0-or-later as an application. Models you *create with* it
  are not covered by the GPL — output is not a derivative work of the tool. This
  is the widely-held reading and Blender Foundation's own position, but it is
  your call for your jurisdiction.
- **OpenSCAD** is GPL-2.0. Same reasoning: `.scad` source you write is yours;
  the OpenSCAD program is not.
- **CadQuery / OCCT** — Apache-2.0 / LGPL-2.1-with-exception respectively. Using
  them to produce STEP files does not encumber your parts.

**Hosted text-to-3D services** are the real risk area. Terms differ substantially
and change often — typically on:

- whether commercial use is included at your tier
- whether you own the output or receive a licence to it
- whether the provider may train on your prompts and outputs
- whether outputs of similar prompts are exclusive to you (usually not)

forge3d cannot answer these for you and does not try. Before shipping hosted
output commercially: read your provider's current terms, keep a record of the
prompt and the generation date, and note the provider in the handoff packet.

**Reference images** carry their own rights. "Image-to-3D" of a copyrighted
design produces a derivative of that design. forge3d records `references` in the
spec precisely so this is auditable later.

## Attribution in deliverables

`create_handoff` has a `notes` field — put the provenance there:

```
- generated with forge3d v0.1.0, backend=procedural, 2026-11-01
- reference: client-supplied photo (rights: client)
- no third-party 3D assets used
```

That one block answers 90% of the questions a legal or procurement review will
ask six months later.
