"""Turn a model into a single self-contained HTML page.

The output is one file with the GLB embedded as a data URI, so a designer can be
sent a link and nothing has to be hosted. For embedding in an existing app, use
`viewer_assets/forge3d-viewer.js` instead — a custom element that takes a URL.

Two honesty notes that used to be wrong in the docs:

* The *model* is embedded and needs no server. The *renderer* (three.js) is
  loaded from a CDN by default, so the page needs network access unless you point
  `FORGE3D_THREE_URL` at a local mirror of three. Air-gapped use is supported, but
  it has to be configured — it is not the default.
* `embed=False` writes the mesh beside the HTML and links it relatively. That
  copy is now actually made; it previously produced a page pointing at a file
  that was never written.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import os
import shutil
from pathlib import Path

from . import meshio
from .platform_support import atomic_write
from .spec import ModelSpec

ASSETS = Path(__file__).parent / "viewer_assets"
TEMPLATE = ASSETS / "template.html"
SCENE_TEMPLATE = ASSETS / "scene_template.html"

DEFAULT_THREE_URL = "https://unpkg.com/three@0.169.0"

MIME = {
    "glb": "model/gltf-binary",
    "gltf": "model/gltf+json",
    "stl": "model/stl",
    "obj": "model/obj",
    "ply": "model/ply",
}


def three_url() -> str:
    """Where the page should load three.js from.

    Set `FORGE3D_THREE_URL` to a local mirror (e.g. `http://localhost:8080/three`)
    for air-gapped deployments, or to a different CDN if unpkg is blocked.
    """
    return (os.environ.get("FORGE3D_THREE_URL") or DEFAULT_THREE_URL).rstrip("/")


def _as_glb(model_path: Path, workdir: Path) -> Path:
    """The viewer needs GLB; convert anything else once, next to the source."""
    if model_path.suffix.lower() == ".glb":
        return model_path
    glb = workdir / (model_path.stem + ".glb")
    if glb.exists() and glb.stat().st_mtime >= model_path.stat().st_mtime:
        return glb
    return meshio.export(meshio.load(model_path), glb)


def _json_for_script(payload: dict) -> str:
    """JSON that is safe to inline inside a <script> element.

    `json.dumps` alone lets a model name containing `</script>` terminate the
    script block early — a real break, and an XSS vector if the name came from
    user input. Escaping `<`, `>` and `&` as unicode escapes is valid JSON and
    cannot close the tag.
    """
    text = json.dumps(payload)
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


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
        # The page links the mesh relatively, so the mesh has to be next to it.
        if glb.resolve() != (out_html.parent / glb.name).resolve():
            target = out_html.parent / glb.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(glb, target)
        uri = glb.name

    raw_title = title or payload_meta["name"] or "forge3d viewer"
    page = TEMPLATE.read_text(encoding="utf-8")
    page = (
        page.replace("__TITLE__", html_lib.escape(raw_title))
        .replace("__MODEL_URI__", uri)
        .replace("__META_JSON__", _json_for_script(payload_meta))
        .replace("__THREE_BASE__", three_url())
    )
    atomic_write(out_html, page)
    return out_html


def build_scene_viewer(
    items: list[dict],
    out_html: str | Path,
    title: str = "forge3d scene",
    meta: dict | None = None,
    starfield: bool = True,
) -> Path:
    """Build a single-file page containing **several** models, positioned in one scene.

    The single-model viewer shows one GLB. A website hero scene needs a composed
    scene: four planets at different positions and scales, one shared sun, a
    starfield. That is this function.

    `items` is a list of dicts:
        path        .glb (or any convertible mesh) — required
        name        label
        position    [x, y, z] in world units
        scale       uniform scale
        rotation    [rx, ry, rz] in radians

    Every mesh is embedded as a data URI, so the page needs no server, no CORS
    and no hosting. As with the single-model viewer, three.js itself still loads
    from a CDN unless FORGE3D_THREE_URL points at a local mirror.
    """
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    payload = []
    for index, item in enumerate(items):
        src = Path(str(item.get("path", "")))
        if not src.is_file():
            raise FileNotFoundError(f"scene item {index}: no such file {src}")
        glb = _as_glb(src, out_html.parent)
        b64 = base64.b64encode(glb.read_bytes()).decode("ascii")
        payload.append({
            "name": item.get("name") or glb.stem,
            "uri": f"data:{MIME.get(glb.suffix.lstrip('.').lower(), 'application/octet-stream')};base64,{b64}",
            "position": list(item.get("position") or [0, 0, 0]),
            "scale": float(item.get("scale", 1.0)),
            "rotation": list(item.get("rotation") or [0, 0, 0]),
        })

    payload_meta = {"name": title, "bodies": len(payload), "source": "forge3d"}
    if meta:
        payload_meta.update(meta)

    page = SCENE_TEMPLATE.read_text(encoding="utf-8")
    page = (
        page.replace("__TITLE__", html_lib.escape(title))
        .replace("__SCENE_JSON__", _json_for_script_list(payload))
        .replace("__META_JSON__", _json_for_script(payload_meta))
        .replace("__THREE_BASE__", three_url())
    )
    atomic_write(out_html, page)
    return out_html


def _json_for_script_list(payload: list) -> str:
    """Same escaping rules as `_json_for_script`, for a list payload."""
    text = json.dumps(payload)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


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
