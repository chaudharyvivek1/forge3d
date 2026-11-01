"""Example: a bevelled gear, built headlessly with Blender's bpy API.

    forge3d create --name gear --purpose game --backend blender \
        --source examples/blender_gear.py

forge3d runs this with `blender -b --factory-startup -P script.py -- out.glb`
and the export path arrives as sys.argv[-1]. If the script does not export,
forge3d appends a GLB export for you — but being explicit is better, because you
control the axes and the material.
"""

import bpy
import sys
import math

OUT = sys.argv[-1]

TEETH = 24
RADIUS = 2.0
DEPTH = 0.4
BEVEL = 0.02

# --- clean slate -----------------------------------------------------------
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

# --- gear ------------------------------------------------------------------
bpy.ops.mesh.primitive_gear(
    number_of_teeth=TEETH,
    radius=RADIUS,
    depth=DEPTH,
    add_ends=True,
)
gear = bpy.context.object
gear.name = "gear"

# bevel: this is why Blender is the right engine for display assets —
# an unbevelled gear catches no light and reads as a flat silhouette.
bev = gear.modifiers.new("bevel", "BEVEL")
bev.width = BEVEL
bev.segments = 3
bev.limit_method = "ANGLE"
bev.angle_limit = math.radians(40)

# subdivision would round the teeth away; a weighted normal keeps the faces
# flat while letting the bevel shade smoothly.
wn = gear.modifiers.new("weighted_normal", "WEIGHTED_NORMAL")
wn.keep_sharp = True

# --- material --------------------------------------------------------------
mat = bpy.data.materials.new("brushed_steel")
mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.62, 0.65, 0.70, 1.0)
bsdf.inputs["Metallic"].default_value = 1.0
bsdf.inputs["Roughness"].default_value = 0.32
gear.data.materials.append(mat)

# --- export ----------------------------------------------------------------
bpy.ops.object.select_all(action="DESELECT")
gear.select_set(True)
bpy.context.view_layer.objects.active = gear

bpy.ops.export_scene.gltf(
    filepath=OUT,
    export_format="GLB",
    use_selection=True,
    export_apply=True,      # bake modifiers into the exported mesh
    export_yup=True,        # glTF convention: Y up
)
print(f"forge3d: exported {OUT}")
