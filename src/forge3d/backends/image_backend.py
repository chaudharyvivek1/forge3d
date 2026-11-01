"""Image-to-3D — deterministic geometry from a real image file.

This is the honest local answer to "image to 3D". A neural image-to-3D model
(TRELLIS, Stable Fast 3D, Hunyuan3D) needs a GPU and gigabytes of weights; what
ships here is the part that actually runs everywhere: **measured geometry derived
from pixels**. Three modes, all real, all reproducible:

  relief      luminance becomes height. A plaque, a lithophane, a coin face, a
              terrain tile from a height map. Watertight solid with a base.
  silhouette  alpha (or luminance) becomes an extruded profile. A logo, a gasket,
              a sign, a cookie cutter. Holes fall out of the mask for free.
  auto        transparent background -> silhouette; otherwise relief.

What this is not: it does not invent the back of an object, does not hallucinate
a person from a photo, and does not texture anything. For that you need a hosted
provider — and `forge3d doctor` plus docs/backends.md say so plainly instead of
pretending this backend is something it isn't.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import numpy as np
import trimesh

from ..spec import ModelSpec
from .base import Backend, BackendResult


class ImageFetchRefused(RuntimeError):
    """A remote image was refused by the URL guard, with a reason."""


MODES = ("auto", "relief", "silhouette")
DEFAULT_GRID = 192
MIN_GRID = 32
MAX_GRID = 512


class ImageBackend(Backend):
    name = "image"
    kind = "local"
    requires: tuple[str, ...] = ("PIL", "numpy", "trimesh")
    capabilities = {
        "input": ["image_path", "image_url"],
        "deterministic": True,
        "parametric": True,
        "textures": False,
        "modes": list(MODES),
        "best_for": [
            "lithophanes and relief plaques",
            "logos and silhouettes extruded to a profile",
            "terrain and height maps",
            "reproducible geometry from a photo you already have",
        ],
        "weak_for": [
            "reconstructing hidden geometry",
            "organic full-body models",
            "textured output",
        ],
        "note": "derives geometry from pixels; it does not hallucinate what is not in the image",
    }

    def available(self) -> bool:
        try:
            import PIL  # noqa: F401
        except Exception:
            return False
        return True

    # -- entry point -----------------------------------------------------
    def generate(
        self,
        spec: ModelSpec,
        out_dir: Path,
        source: str | None = None,
        **kwargs,
    ) -> BackendResult:
        if not self.available():
            return BackendResult(
                ok=False,
                error="PIL is not installed",
                log="pip install pillow",
            )

        try:
            image_path = self._resolve_image(spec, source)
        except ImageFetchRefused as exc:
            return BackendResult(
                ok=False,
                error=f"refused to fetch that image: {exc}",
                log="Remote images are restricted to public http(s) hosts with a size "
                    "and pixel budget. Download it yourself and pass a local path if it "
                    "is on a private network.",
            )
        if image_path is None:
            return BackendResult(
                ok=False,
                error="the image backend needs an image: pass `source` as a path/URL, "
                      "or put one in spec.references",
                log="Example: create_model(backend='image', source='/path/logo.png', "
                    "purpose='print')",
            )

        try:
            img = self._load_image(image_path)
        except Exception as exc:  # noqa: BLE001 - reported to the agent
            return BackendResult(ok=False, error=f"could not read image: {exc}")

        mode = (kwargs.get("mode") or "auto").lower()
        if mode not in MODES:
            mode = "auto"
        resolved = self._resolve_mode(img, mode)

        requested_grid = int(kwargs.get("grid") or DEFAULT_GRID)
        grid = clamp_grid(requested_grid, spec.poly_budget)

        try:
            if resolved == "silhouette":
                mesh, meta = self._silhouette(img, spec, grid, kwargs)
            else:
                mesh, meta = self._relief(img, spec, grid, kwargs)
        except Exception as exc:  # noqa: BLE001
            return BackendResult(ok=False, error=f"{resolved} build failed: {exc}")

        if mesh is None or len(mesh.faces) == 0:
            return BackendResult(
                ok=False,
                error="the image produced no geometry",
                log="An empty mask or a blank image yields nothing. Check the alpha "
                    "channel, or pass threshold= to loosen the silhouette cut.",
            )

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        glb = out_dir / f"{spec.name}.glb"
        stl = out_dir / f"{spec.name}.stl"
        mesh.export(str(glb))
        mesh.export(str(stl))

        meta.update({
            "engine": "image",
            "requested_grid": requested_grid,
            "mode": resolved,
            "source_image": str(image_path),
            "faces": int(len(mesh.faces)),
            "extents": [round(float(e), 3) for e in mesh.extents],
        })
        return BackendResult(
            ok=True,
            path=str(glb),
            extra_outputs={"stl": str(stl)},
            meta=meta,
            log=(
                f"image -> {resolved} at {meta['grid']}x{meta['grid']} grid. "
                "This is geometry derived from pixels, not a reconstruction — "
                "run validate_model against your delivery profile."
            ),
        )

    # -- input -----------------------------------------------------------
    @staticmethod
    def _resolve_image(spec: ModelSpec, source: str | None) -> Path | None:
        candidate = source or (spec.references[0] if spec.references else None)
        if not candidate:
            return None
        if str(candidate).startswith(("http://", "https://")):
            # Guarded fetch: SSRF checks, redirect revalidation, byte ceiling and
            # a real image decode. `requests.get(url)` + `.content` — the obvious
            # implementation — will happily read cloud metadata or a 4 GB file.
            import tempfile

            from ..netguard import UnsafeURLError, fetch_image

            try:
                tmp_dir = Path(tempfile.mkdtemp(prefix="forge3d-image-"))
                return fetch_image(str(candidate), tmp_dir / "input.png")
            except UnsafeURLError as exc:
                raise ImageFetchRefused(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise ImageFetchRefused(f"could not fetch {candidate}: {exc}") from exc
        p = Path(candidate).expanduser()
        return p if p.is_file() else None

    @staticmethod
    def _load_image(path: Path):
        from PIL import Image

        img = Image.open(path)
        img.load()
        return img.convert("RGBA")

    @staticmethod
    def _resolve_mode(img, mode: str) -> str:
        if mode != "auto":
            return mode
        alpha = np.asarray(img)[:, :, 3]
        # A meaningful transparent background means the image is a cut-out, which
        # is exactly the case silhouette extrusion is for.
        transparent = float((alpha < 128).mean())
        return "silhouette" if transparent > 0.05 else "relief"

    # -- geometry --------------------------------------------------------
    def _grid_arrays(self, img, grid: int):
        from PIL import Image

        small = img.resize((grid, grid), Image.LANCZOS)
        arr = np.asarray(small).astype(np.float64) / 255.0
        rgb, alpha = arr[:, :, :3], arr[:, :, 3]
        luminance = rgb @ np.array([0.2126, 0.7152, 0.0722])
        return luminance, alpha

    def _relief(self, img, spec: ModelSpec, grid: int, opts: dict):
        """Luminance as height, on a solid base — a lithophane/plaque."""
        luminance, alpha = self._grid_arrays(img, grid)

        max_height = float(opts.get("height") or self._default_height(spec))
        base = float(opts.get("base") or max(0.6, max_height * 0.18))
        invert = bool(opts.get("invert", False))

        # Brighter = taller unless inverted. Transparent pixels contribute nothing.
        shade = (1.0 - luminance) if invert else luminance
        shade = np.where(alpha < 0.5, 0.0, shade)

        # A light blur stops a noisy photo from producing a spiky, unprintable
        # surface; it is a geometric decision, not a cosmetic one.
        smooth = int(opts.get("smooth") or 1)
        if smooth > 0:
            from scipy.ndimage import gaussian_filter

            shade = gaussian_filter(shade, sigma=smooth)

        heights = (shade * max_height).astype(np.float64)
        mesh = self._heightfield_solid(heights, base)
        meta = {"grid": grid, "max_height": max_height, "base": base,
                "invert": invert, "smooth": smooth}
        return self._fit(mesh, spec), meta

    def _silhouette(self, img, spec: ModelSpec, grid: int, opts: dict):
        """Threshold the image, extrude the mask to a thickness."""
        luminance, alpha = self._grid_arrays(img, grid)
        threshold = float(opts.get("threshold") or 0.5)

        has_alpha = bool((alpha < 0.5).any())
        mask = alpha >= threshold if has_alpha else luminance >= threshold

        if not mask.any():
            return None, {}

        thickness = float(opts.get("thickness") or self._default_thickness(spec))
        mesh = self._extrude_mask(mask, thickness)
        if mesh is None or len(mesh.faces) == 0:
            return None, {}

        mesh.apply_translation([-grid / 2.0, -grid / 2.0, 0.0])
        mesh.merge_vertices()
        meta = {"grid": grid, "thickness": thickness, "threshold": threshold}
        return self._fit(mesh, spec), meta

    @staticmethod
    def _extrude_mask(mask: np.ndarray, thickness: float) -> trimesh.Trimesh | None:
        """Extrude a boolean mask into a closed solid by emitting only the
        *exposed* faces of each filled cell.

        This replaces `trimesh.voxel.ops.multibox`, whose signature changed in
        trimesh 5.x (`pitch` became a 3-vector and it raised ValueError on the
        scalar form). Doing it here is also better geometry: interior faces are
        never emitted, so the result is a clean manifold rather than a pile of
        boxes welded together, and holes in the mask survive as real holes.
        """
        rows, cols = mask.shape
        if not mask.any():
            return None

        corner = np.arange((rows + 1) * (cols + 1)).reshape(rows + 1, cols + 1)
        offset = (rows + 1) * (cols + 1)

        xs = np.tile(np.arange(cols + 1, dtype=float), rows + 1)
        ys = np.repeat(np.arange(rows + 1, dtype=float), cols + 1)
        top_verts = np.column_stack([xs, ys, np.full(xs.size, float(thickness))])
        bottom_verts = np.column_stack([xs, ys, np.zeros(xs.size)])
        vertices = np.vstack([top_verts, bottom_verts])

        i, j = np.nonzero(mask)
        a = corner[i, j]
        b = corner[i, j + 1]
        c = corner[i + 1, j + 1]
        d = corner[i + 1, j]

        faces = [
            np.column_stack([a, b, c]),
            np.column_stack([a, c, d]),
            np.column_stack([a + offset, c + offset, b + offset]),
            np.column_stack([a + offset, d + offset, c + offset]),
        ]

        padded = np.pad(mask, 1, constant_values=False)

        def side(empty_neighbour: np.ndarray, edge_a, edge_b):
            """Quad strip where a filled cell borders an empty neighbour."""
            ti0, ti1 = edge_a[empty_neighbour], edge_b[empty_neighbour]
            bi0, bi1 = ti0 + offset, ti1 + offset
            faces.append(np.column_stack([ti0, ti1, bi1]))
            faces.append(np.column_stack([ti0, bi1, bi0]))

        # north / south / west / east. `a`..`d` describe only the filled cells
        # (length = mask.sum()), so the neighbour lookups are indexed at (i, j)
        # rather than over the whole grid.
        side(~padded[0:rows, 1:cols + 1][i, j], a, b)
        side(~padded[2:rows + 2, 1:cols + 1][i, j], d, c)
        side(~padded[1:rows + 1, 0:cols][i, j], a, d)
        side(~padded[1:rows + 1, 2:cols + 2][i, j], b, c)

        mesh = trimesh.Trimesh(vertices=vertices, faces=np.vstack(faces), process=True)
        mesh.merge_vertices()
        mesh.remove_unreferenced_vertices()
        trimesh.repair.fix_normals(mesh)
        return mesh

    @staticmethod
    def _heightfield_solid(heights: np.ndarray, base: float) -> trimesh.Trimesh:
        """A watertight solid: top surface, skirt, flat bottom."""
        rows, cols = heights.shape
        xs, ys = np.meshgrid(np.arange(cols, dtype=float), np.arange(rows, dtype=float))

        top = np.column_stack([xs.ravel(), ys.ravel(), heights.ravel()])
        bottom = np.column_stack([xs.ravel(), ys.ravel(), np.full(rows * cols, -base)])
        vertices = np.vstack([top, bottom])
        offset = rows * cols

        def quads(idx: np.ndarray) -> np.ndarray:
            a = idx[:-1, :-1].ravel()
            b = idx[:-1, 1:].ravel()
            c = idx[1:, 1:].ravel()
            d = idx[1:, :-1].ravel()
            return np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])])

        grid_idx = np.arange(rows * cols).reshape(rows, cols)
        faces = [quads(grid_idx), quads(grid_idx + offset)[:, ::-1]]

        # Skirt: four borders, each a strip of quads joining top to bottom.
        def wall(top_edge, bottom_edge):
            for i in range(len(top_edge) - 1):
                t0, t1 = top_edge[i], top_edge[i + 1]
                b0, b1 = bottom_edge[i] + offset, bottom_edge[i + 1] + offset
                faces.append(np.array([[t0, t1, b1], [t0, b1, b0]]))

        wall(grid_idx[0, :], grid_idx[0, :])              # front
        wall(grid_idx[-1, ::-1], grid_idx[-1, ::-1])      # back
        wall(grid_idx[::-1, 0], grid_idx[::-1, 0])        # left
        wall(grid_idx[:, -1], grid_idx[:, -1])            # right

        mesh = trimesh.Trimesh(vertices=vertices, faces=np.vstack(faces), process=True)
        mesh.merge_vertices()
        # Skirt quads are wound per-border, so the assembled solid can end up
        # with mixed orientation. `fix_winding` needs networkx (a declared
        # dependency now); signed volume then settles global inside-out-ness.
        with contextlib.suppress(Exception):  # pragma: no cover - networkx absent
            trimesh.repair.fix_winding(mesh)
        if mesh.volume < 0:
            mesh.invert()
        return mesh

    # -- helpers ---------------------------------------------------------
    def _default_height(self, spec: ModelSpec) -> float:
        if spec.extent:
            return max(0.5, max(spec.extent) * 0.08)
        return 4.0

    def _default_thickness(self, spec: ModelSpec) -> float:
        if spec.extent:
            return max(0.4, max(spec.extent) * 0.06)
        return 3.0

    @staticmethod
    def _fit(mesh: trimesh.Trimesh, spec: ModelSpec) -> trimesh.Trimesh:
        """Scale to the brief's dominant dimension, keeping aspect ratio."""
        if spec.extent:
            target = max(spec.extent)
            current = float(max(mesh.extents))
            if current > 0 and abs(target - current) > 1e-9:
                mesh.apply_scale(target / current)
        return mesh


def image_size(path: str | Path) -> tuple[int, int] | None:
    """Convenience for callers that want to report the source resolution."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def clamp_grid(value: int, poly_budget: int | None = None) -> int:
    """Bound the sampling grid, and shrink it to fit a polygon budget.

    A 512x512 relief is ~1.05M triangles and takes minutes to build. That is a
    production hazard, not a test problem: an agent that asks for a 4k image and
    a 60k-triangle game asset should get a model inside its budget, not a
    timeout and a 17x overshoot. Solving the face-count formula for the grid is
    cheaper and more honest than generating and then decimating.
    """
    grid = max(MIN_GRID, min(MAX_GRID, int(value)))
    if not poly_budget or poly_budget <= 0:
        return grid
    # 4*(g-1)^2 + 8*(g-1) <= budget  ->  solve for g
    while grid > MIN_GRID and triangle_estimate(grid) > poly_budget:
        grid = max(MIN_GRID, int(grid * 0.8))
    return grid


def triangle_estimate(grid: int) -> int:
    """Exact face count for a relief solid at this grid.

    Top surface + bottom surface (2 triangles per cell each) + a skirt of
    2 triangles per edge on all four borders. Getting this right matters: it is
    what lets an agent check a polygon budget *before* spending a minute
    generating a 512x512 relief that will never fit.
    """
    cells = (grid - 1) ** 2
    skirt = 2 * 4 * (grid - 1)
    return int(4 * cells + skirt)
