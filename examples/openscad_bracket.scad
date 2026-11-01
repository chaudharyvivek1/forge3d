// Example: a wall bracket for a 32 mm pipe, in OpenSCAD.
//
//   forge3d create --name bracket --purpose print --backend openscad \
//       --source examples/openscad_bracket.scad --dims x=120,y=60,z=40
//
// Everything is a parameter. Change PIPE_D for a different pipe and the whole
// part re-derives — which is the reason to use CSG for hard-surface work.

PIPE_D   = 32;     // pipe outer diameter, mm
CLEAR    = 0.4;    // clearance so the pipe actually fits, mm
WALL     = 6;      // wall thickness, mm
W        = 120;    // overall width
D        = 60;     // overall depth
H        = 40;     // overall height
HOLE_D   = 4.5;    // M4 clearance
HOLE_X   = 40;     // hole spacing (both axes)
CHAMFER  = 1.2;

$fn = 48;

module saddle() {
    difference() {
        union() {
            // body
            hull() {
                cube([W, D, H], center = true);
            }
            // outer barrel so the pipe seats in a round cradle
            translate([0, 0, H / 2 - WALL])
                rotate([90, 0, 0])
                    cylinder(d = PIPE_D + 2 * WALL, h = D, center = true);
        }

        // pipe channel
        translate([0, 0, H / 2 - WALL])
            rotate([90, 0, 0])
                cylinder(d = PIPE_D + CLEAR, h = D + 2, center = true);

        // mounting holes
        for (x = [-HOLE_X / 2, HOLE_X / 2]) {
            translate([x, 0, 0])
                cylinder(d = HOLE_D, h = H + 2, center = true);
        }

        // relieve the underside so it sits flat against a wall
        translate([0, 0, -H / 2 - 0.01])
            cube([W + 2, D * 0.55, WALL], center = true);
    }
}

module chamfered() {
    // a light chamfer reads as "manufactured" rather than "extruded"
    difference() {
        saddle();
        translate([0, 0, H / 2])
            cube([W + 2, D + 2, 2 * CHAMFER], center = true);
    }
}

chamfered();
