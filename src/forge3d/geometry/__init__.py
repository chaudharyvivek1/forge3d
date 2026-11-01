"""Reusable procedural geometry generators.

The point of this package is that a *shape family* (planet, terrain, rock) is
implemented once, with parameters and tests, instead of being re-derived in every
agent's model source. See `planets.py`.
"""

from .planets import PlanetParams, generate_planet, generate_scene

__all__ = ["PlanetParams", "generate_planet", "generate_scene"]
