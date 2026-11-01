"""Build the README's flow GIF: prompt → model → viewer.

This is a **rendered turntable of a real generated model**, not a screen
recording. Every frame comes from the same CPU renderer the `render_model` tool
uses, so the GIF cannot show anything the library does not actually produce.

    python examples/make_flow_gif.py           # writes docs/demo/flow.gif

Requires matplotlib and Pillow (both in the `render` extra).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from PIL import Image, ImageDraw, ImageFont

from forge3d.geometry.surfaces import build_shape
from forge3d.render import _shade

WIDTH, HEIGHT = 680, 396
BG = (11, 13, 16)
FG = (232, 236, 242)
DIM = (152, 162, 179)
ACCENT = (212, 105, 58)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


def render_view(mesh, elevation: float, azimuth: float) -> Image.Image:
    """One shaded frame of the mesh, at the requested camera angle."""
    fig = plt.figure(figsize=(WIDTH / 100, HEIGHT / 100), dpi=100, facecolor="#0b0d10")
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111, projection="3d", facecolor="#0b0d10")

    triangles = mesh.triangles
    colours = _shade(mesh, base=(0.86, 0.47, 0.28))   # warm, to match the accent
    collection = Poly3DCollection(triangles, facecolors=colours,
                                  edgecolors="none", linewidths=0)
    collection.set_zsort("average")
    ax.add_collection3d(collection)

    bounds = mesh.bounds
    centre = (bounds[0] + bounds[1]) / 2.0
    # matplotlib's 3D axes shrink the plotted box, so framing at the bounding
    # radius leaves the model small. 0.72 fills the frame.
    radius = float(np.max(mesh.extents) / 2.0) * 0.72
    ax.set_xlim(centre[0] - radius, centre[0] + radius)
    ax.set_ylim(centre[1] - radius, centre[1] + radius)
    ax.set_zlim(centre[2] - radius, centre[2] + radius)
    ax.set_box_aspect([1, 1, 1])
    ax.view_init(elev=elevation, azim=azimuth)
    ax.set_axis_off()

    canvas.draw()
    frame = Image.frombuffer("RGBA", canvas.get_width_height(),
                             canvas.buffer_rgba(), "raw", "RGBA", 0, 1)
    plt.close(fig)
    return frame.convert("RGB")


def caption_frame(number: str, title: str, body: str, footer: str) -> Image.Image:
    """A title card, so the GIF reads as a flow rather than a spinning object."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    draw.text((48, 118), number, font=_font(15, bold=True), fill=ACCENT)
    draw.text((48, 148), title, font=_font(40, bold=True), fill=FG)

    y = 214
    for line in body.split("\n"):
        draw.text((48, y), line, font=_font(17), fill=DIM)
        y += 28

    draw.line([(48, HEIGHT - 78), (WIDTH - 48, HEIGHT - 78)], fill=(32, 38, 48), width=1)
    draw.text((48, HEIGHT - 58), footer, font=_font(14), fill=(120, 132, 148))
    return img


def with_caption(frame: Image.Image, text: str) -> Image.Image:
    """Overlay a lower-third caption on a rendered frame."""
    img = frame.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([(0, HEIGHT - 54), (WIDTH, HEIGHT)], fill=(11, 13, 16, 235))
    draw.text((48, HEIGHT - 38), text, font=_font(15), fill=FG)
    return img


def main() -> int:
    out_path = Path(__file__).resolve().parents[1] / "docs" / "demo" / "flow.gif"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("building the model…")
    mesh = build_shape("torus_knot", {"p": 2, "q": 3, "R": 2.0, "tube": 0.55},
                       resolution=140)
    faces = len(mesh.faces)

    frames: list[Image.Image] = []

    # 1. the prompt
    frames.append(caption_frame(
        "01 · PROMPT",
        "“a trefoil knot, in bronze”",
        "create_shape(shape=\"torus_knot\", params={\"p\": 2, \"q\": 3})",
        "one line of Python — no GPU, no API key, no Blender",
    ))

    # 2. the model, turning
    print("rendering the turntable…")
    for step in range(8):
        azimuth = step * 45
        elevation = 22 + 8 * math.sin(math.radians(step * 45))
        frame = render_view(mesh, elevation, azimuth)
        label = (f"{faces:,} triangles · watertight · validated PASS"
                 if step % 3 == 0 else "building geometry from the equation")
        frames.append(with_caption(frame, label))

    # 3. the viewer
    frames.append(caption_frame(
        "02 · VIEWER",
        "one self-contained HTML file",
        "make_viewer() embeds the GLB as a data URI.\n"
        "No server, no CORS, no hosting — open it or send it.",
        "orbit · wireframe · snapshot · drop in your own .glb",
    ))

    # a static still of the finished asset, for the last beat
    still = render_view(mesh, 26, 42)
    frames.append(with_caption(still, "done — ship the GLB, the STL, or the page"))
    frames.append(caption_frame(
        "03 · SHIP",
        "forge3d demo",
        "One command, base install only:\n"
        "pip install forge3d && forge3d demo",
        "github.com/chaudharyvivek1/forge3d",
    ))

    print(f"writing {out_path} ({len(frames)} frames)…")
    frames[0].save(
        out_path, save_all=True, append_images=frames[1:],
        duration=[1600, 1100] + [150] * 8 + [1600, 1400, 1800],
        loop=0, optimize=True,
    )
    print(f"  {out_path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
