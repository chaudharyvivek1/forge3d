"""Turn a model into a single self-contained HTML page.

The output is one file with the GLB embedded as a data URI. That matters: it
means a designer can be sent a link, a stakeholder can open it offline, and
nothing has to be hosted. For embedding in an existing app, use
`viewer_assets/forge3d-viewer.js` instead — a custom element that takes a URL.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from . import meshio
from .spec import ModelSpec

ASSETS = Path(__file__).parent / "viewer_assets"
TEMPLATE = ASSETS / "template.html"
MIME = {
    "glb": "model/gltf-binary",
    "gltf": "model/gltf+json",
    "stl": "model/stl",
    "obj": "model/obj",
    "ply": "model/ply",
}


def _as_glb(model_path: Path, workdir: Path) -> Path:
    """The viewer needs GLB; convert anything else once, next to the source."""
    if model_path.suffix.lower() == ".glb":
        return model_path
    glb = workdir / (model_path.stem + ".glb")
    if glb.exists() and glb.stat().st_mtime >= model_path.stat().st_mtime:
        return glb
    return meshio.export(meshio.load(model_path), glb)


def build_viewer(
    model_path: str | Path,
    out_html: str | Path,
    title: str | None = None,
    spec: ModelSpec | None = None,
    meta: dict | None = None,
    embed: bool = True,
) -> Path:
    model_path = Path(model_path)
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    if not model_path.exists():
        raise FileNotFoundError(model_path)

    glb = _as_glb(model_path, out_html.parent)

    payload_meta = {
        "name": (spec.name if spec else model_path.stem),
        "units": (spec.units if spec else None),
        "source": glb.name,
    }
    if spec is not None:
        payload_meta["purpose"] = spec.purpose
        payload_meta["target_dims"] = spec.extent
    if meta:
        payload_meta.update(meta)

    if embed:
        b64 = base64.b64encode(glb.read_bytes()).decode("ascii")
        uri = f"data:{MIME.get(glb.suffix.lstrip('.').lower(), 'application/octet-stream')};base64,{b64}"
    else:
        uri = glb.name

    html = TEMPLATE.read_text(encoding="utf-8")
    html = (
        html.replace("__TITLE__", (title or payload_meta["name"] or "forge3d viewer"))
        .replace("__MODEL_URI__", uri)
        .replace("__META_JSON__", json.dumps(payload_meta))
    )
    out_html.write_text(html, encoding="utf-8")
    return out_html


def viewer_from_file(src: str | Path, out_html: str | Path, **kwargs) -> Path:
    return build_viewer(src, out_html, **kwargs)


def copy_web_component(out_dir: str | Path) -> Path:
    """Copy the embeddable custom element next to a page for local use."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = ASSETS / "forge3d-viewer.js"
    dst = out_dir / "forge3d-viewer.js"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst
