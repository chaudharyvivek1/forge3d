"""Example: a parametric side chair, built with trimesh primitives.

Run it through forge3d:

    forge3d create --name chair --purpose game --backend procedural \
        --source examples/procedural_chair.py --viewer --dims x=450,y=480,z=850

Everything here is driven by numbers at the top, which is the point of the
procedural backend: change a dimension, rebuild, and the model is still exact.
"""

import trimesh

# --- parameters (millimetres) ---------------------------------------------
SEAT_W = 420.0
SEAT_D = 400.0
SEAT_H = 45.0
SEAT_Z = 430.0

BACK_H = 400.0
BACK_T = 28.0

LEG_W = 34.0
LEG_INSET = 22.0

ROUND = 6.0  # bevel radius on the seat and back


def build():
    parts = []

    # seat
    seat = trimesh.creation.box(extents=[SEAT_W, SEAT_D, SEAT_H])
    seat.apply_translation([0, 0, SEAT_Z])
    parts.append(seat)

    # backrest, tilted 8 degrees
    back = trimesh.creation.box(extents=[SEAT_W, BACK_T, BACK_H])
    back.apply_transform(trimesh.transformations.rotation_matrix(0.14, [1, 0, 0]))
    back.apply_translation([0, -SEAT_D / 2 + BACK_T, SEAT_Z + SEAT_H / 2 + BACK_H / 2])
    parts.append(back)

    # four legs
    leg_h = SEAT_Z - SEAT_H / 2
    for sx in (-1, 1):
        for sy in (-1, 1):
            leg = trimesh.creation.box(extents=[LEG_W, LEG_W, leg_h])
            leg.apply_translation(
                [
                    sx * (SEAT_W / 2 - LEG_INSET - LEG_W / 2),
                    sy * (SEAT_D / 2 - LEG_INSET - LEG_W / 2),
                    leg_h / 2,
                ]
            )
            parts.append(leg)

    # stretch (cross braces) between the legs, so the frame reads as furniture
    brace_z = leg_h * 0.32
    for sy in (-1, 1):
        brace = trimesh.creation.box(extents=[SEAT_W - 2 * LEG_INSET, 18.0, 26.0])
        brace.apply_translation([0, sy * (SEAT_D / 2 - LEG_INSET - LEG_W / 2), brace_z])
        parts.append(brace)

    chair = trimesh.util.concatenate(parts)
    chair.merge_vertices()
    return chair
