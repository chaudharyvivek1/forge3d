"""Reusable procedural geometry generators.

The point of this package is that a *shape family* is implemented once, with
parameters, equations and tests, instead of being re-derived in every agent's
model source.

    planets.py    noise terrain, craters, canyons, baked PBR maps
    surfaces.py   parametric surfaces defined by an equation
"""

from .planets import PlanetParams, generate_planet, generate_scene
from .surfaces import SHAPES, build_shape, list_shapes

__all__ = [
    "PlanetParams", "generate_planet", "generate_scene",
    "SHAPES", "build_shape", "list_shapes",
]
