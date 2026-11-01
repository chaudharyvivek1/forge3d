"""Example: a machined mounting plate in CadQuery — the STEP route.

    pip install cadquery
    forge3d create --name plate --purpose cad --backend cadquery \
        --source examples/cadquery_plate.py --dims x=120,y=80,z=12

Output is a STEP file (B-rep, what a machine shop wants) plus an STL for
previewing. A mesh-only pipeline cannot produce the STEP.
"""

import cadquery as cq

# --- parameters (mm) -------------------------------------------------------
W, D, T = 120.0, 80.0, 12.0
FILLET = 6.0
HOLE_D = 6.6          # M6 clearance
HOLE_INSET = 14.0
BOSS_D = 22.0
BOSS_H = 9.0
BORE_D = 12.0         # bearing seat
EDGE_BREAK = 1.0

result = (
    cq.Workplane("XY")
    .box(W, D, T)
    # rounded corners — real plates are never sharp; they catch and cut
    .edges("|Z")
    .fillet(FILLET)
    # central boss the bearing presses into
    .faces(">Z")
    .workplane()
    .circle(BOSS_D / 2)
    .extrude(BOSS_H)
    # bearing bore through boss and plate
    .faces(">Z")
    .workplane()
    .hole(BORE_D)
    # four M6 mounting holes
    .faces("<Z")
    .workplane()
    .rect(W - 2 * HOLE_INSET, D - 2 * HOLE_INSET, forConstruction=True)
    .vertices()
    .hole(HOLE_D)
    # break the top edge so it does not feel sharp in the hand
    .edges(">Z")
    .chamfer(EDGE_BREAK)
)

# CadQuery export is handled by the backend; `result` is the contract.
