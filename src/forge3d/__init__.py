"""forge3d — a 3D-model toolchain that any AI agent can drive over MCP.

The package is deliberately split so that each layer is usable on its own:

    spec.py       what the model is supposed to be (the brief, as data)
    backends/     how geometry actually gets made (5 interchangeable engines)
    validate.py   is the result fit for its purpose (print / web / game / cad)
    repair.py     make a broken mesh usable
    render.py     turn a mesh into preview images an agent can *see*
    viewer.py     turn a mesh into a single-file Three.js page anyone can open
    context.py    compact "model card" so a mesh never eats a context window
    handoff.py    multi-agent / multi-human handoff packet + checklist
    server.py     the MCP surface
"""

from .spec import ModelSpec

__version__ = "0.6.3"
__all__ = ["ModelSpec", "__version__"]
