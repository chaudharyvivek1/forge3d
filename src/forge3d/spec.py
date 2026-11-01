"""The model brief, as data.

Every failure mode in agent-generated 3D starts here: a vague prompt becomes a
mesh with no scale, no purpose and no acceptance test. `ModelSpec` forces the
agent to commit to those things *before* any geometry is generated, and it is
the object that gets handed between agents.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

PURPOSES = ("print", "web", "game", "cad", "any")
UNITS = ("mm", "cm", "m", "in")

# Names that are illegal as file or directory names on Windows, whatever the
# extension. A model called "con" or "aux" would fail to create its output
# directory on Windows only — the kind of bug that ships because CI was Linux.
_WIN_RESERVED = {
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _slug(text: str, fallback: str = "model") -> str:
    """A filesystem-safe identifier that keeps non-Latin scripts intact.

    The previous version stripped everything outside `[a-zA-Z0-9]`, so a model
    named "कुर्सी" or "椅子" silently became "model" — a name collision waiting to
    happen for any user not naming things in English.

    Unicode letters, digits **and combining marks** are preserved. Marks matter:
    `re`'s `\\w` excludes them, which turned "कुर्सी" into "करस" — a mangled word
    that looks like a typo in every Indian language that uses matras.
    """
    s = unicodedata.normalize("NFKC", (text or "").strip()).lower()
    kept = []
    for ch in s:
        if ch in "-_ ":
            kept.append(ch)
            continue
        # L* letters, M* marks (matras, vowel signs), N* digits — all valid in
        # filenames on Windows, macOS and Linux.
        if unicodedata.category(ch)[0] in ("L", "M", "N"):
            kept.append(ch)
    s = "".join(kept)
    s = re.sub(r"[\s_]+", "-", s).strip("-.")
    s = s[:60].strip("-.")
    if not s:
        return fallback
    if s.split(".")[0] in _WIN_RESERVED:
        s = f"{s}-model"
    return s


@dataclass
class ModelSpec:
    """A complete, checkable description of the model to build.

    name         short identifier, also used for filenames
    prompt       the creative brief in prose (what it looks like)
    purpose      print | web | game | cad | any — drives the validation profile
    units        mm | cm | m | in — real-world units of `dimensions`
    dimensions   target bounding box, e.g. {"x": 180, "y": 90, "z": 75}
    poly_budget  hard triangle ceiling; None means "use the profile default"
    materials    named materials/slots the downstream engine expects
    constraints  explicit musts, e.g. "watertight", "no supports", "1 body"
    references   image URLs or local paths to match
    """

    name: str = "model"
    prompt: str = ""
    purpose: str = "any"
    style: str = ""
    units: str = "mm"
    dimensions: dict[str, float] | None = None
    poly_budget: int | None = None
    materials: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    seed: int | None = None

    def __post_init__(self) -> None:
        self.name = _slug(self.name or self.prompt)
        self.name = self.name or "model"
        self.purpose = (self.purpose or "any").lower()
        if self.purpose not in PURPOSES:
            self.purpose = "any"
        self.units = (self.units or "mm").lower()
        if self.units not in UNITS:
            self.units = "mm"

    # ---- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> ModelSpec:
        data = dict(data or {})
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})

    # ---- derived -------------------------------------------------------
    @property
    def extent(self) -> list[float] | None:
        if not self.dimensions:
            return None
        return [
            float(self.dimensions.get("x", 0)),
            float(self.dimensions.get("y", 0)),
            float(self.dimensions.get("z", 0)),
        ]

    def prompt_for_backend(self) -> str:
        """A single string a text-to-3D model can consume."""
        bits = [self.prompt or self.name]
        if self.style:
            bits.append(f"style: {self.style}")
        if self.dimensions:
            d = ", ".join(f"{k}={v}{self.units}" for k, v in self.dimensions.items())
            bits.append(f"dimensions: {d}")
        if self.constraints:
            bits.append("constraints: " + "; ".join(self.constraints))
        return ". ".join(bits)

    def missing(self) -> list[str]:
        """What still has to be decided before this is a good brief."""
        gaps = []
        if not (self.prompt or self.name):
            gaps.append("prompt")
        if self.purpose == "any":
            gaps.append("purpose (print/web/game/cad)")
        if not self.dimensions:
            gaps.append("dimensions (real-world size)")
        return gaps

    def summary(self) -> str:
        dims = self.extent
        dim_txt = "x".join(f"{d:g}" for d in dims) + self.units if dims else "unspecified size"
        return f"{self.name} — {self.purpose}, {dim_txt}, budget {self.poly_budget or 'default'} tris"
