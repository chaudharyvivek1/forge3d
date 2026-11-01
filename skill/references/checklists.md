# Checklists by delivery target

Machine-readable versions live in `src/forge3d/handoff.py::CHECKLISTS` and are
served by the `checklist` tool. These are the prose versions — use them when
reviewing a model with a human.

## brief

- [ ] purpose chosen (print / web / game / cad)
- [ ] real-world dimensions in mm recorded
- [ ] style reference captured or explicitly waived
- [ ] polygon budget set
- [ ] commercial-use / licence of any reference or hosted output confirmed

## geometry

- [ ] single source of truth for the mesh (one file, one path)
- [ ] model is a sensible size and orientation (Y-up for web, Z-up for print)
- [ ] origin at the base centre, not the world origin
- [ ] no stray helper objects, cameras or lights in the export
- [ ] scale applied — no non-uniform object scale left in the transform

## print

- [ ] watertight (`validate_model --profile print`)
- [ ] consistent winding, positive volume
- [ ] minimum wall thickness respected for the target process
  (FDM ≈ 0.8 mm, resin ≈ 0.5 mm, SLS ≈ 0.7 mm as rough floors)
- [ ] overhangs reviewed for the chosen orientation
- [ ] exported as binary STL or 3MF at 1:1 scale
- [ ] parts that must move have clearance (≈0.2–0.3 mm per side)

## web

- [ ] exported as GLB, textures embedded
- [ ] triangle count within budget for the target device
- [ ] UVs present, materials use PBR metallic-roughness
- [ ] viewer tested at phone width
- [ ] file size acceptable (roughly under 5 MB for a hero asset, under 1 MB for
      a grid of thumbnails)
- [ ] no server round-trip needed to show the model

## game

- [ ] LOD chain generated (100% / 50% / 20%)
- [ ] collision proxy simpler than the visual mesh
- [ ] pivot and forward axis match engine convention
- [ ] material count minimised, textures atlased where possible
- [ ] rig/skeleton convention agreed if animated
- [ ] scale matches the engine's unit convention (1 unit = 1 m in Unity/Unreal
      by default — not mm)

## delivery

- [ ] final export path recorded in the handoff
- [ ] validation report attached and passing for the target profile
- [ ] preview images attached
- [ ] named owner for the next step
- [ ] licence and attribution note written

## The five-minute review

When someone hands you a model file, do this before anything else:

1. `inspect_model` — triangles, dimensions, watertight, bodies.
2. `render_model` — look at it. Iso and front, at minimum.
3. `validate_model --profile <target>` — read every hint.
4. Sanity-check scale: a chair is ~450 mm wide, a phone ~75 mm, a door ~800 mm.
   If the extents say 0.45, someone used metres in a millimetre pipeline.
5. `model_card` — write the summary down. That is what you paste in the ticket.
