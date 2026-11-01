import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    """Every test gets its own workdir so nothing leaks between runs."""
    work = tmp_path / "forge3d-work"
    monkeypatch.setenv("FORGE3D_WORKDIR", str(work))
    from forge3d import config

    config._settings = None
    yield work
    config._settings = None


@pytest.fixture
def cube():
    import trimesh

    return trimesh.creation.box(extents=[10, 10, 10])


@pytest.fixture
def open_surface():
    """A single quad — the classic 'not watertight' case."""
    import numpy as np
    import trimesh

    verts = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], dtype=float)
    return trimesh.Trimesh(vertices=verts, faces=[[0, 1, 2], [0, 2, 3]], process=False)


@pytest.fixture
def tmp_model(tmp_path, cube):
    p = tmp_path / "cube.glb"
    cube.export(str(p))
    return p


def has_binary(name: str) -> bool:
    from shutil import which

    return which(name) is not None


skip_without_openscad = pytest.mark.skipif(not has_binary("openscad"), reason="openscad not installed")
skip_without_blender = pytest.mark.skipif(not has_binary("blender"), reason="blender not installed")


def has_cadquery() -> bool:
    try:
        import cadquery  # noqa: F401

        return True
    except Exception:
        return False


skip_without_cadquery = pytest.mark.skipif(not has_cadquery(), reason="cadquery not installed")

os.environ.setdefault("FORGE3D_ALLOW_CODE_EXEC", "true")
