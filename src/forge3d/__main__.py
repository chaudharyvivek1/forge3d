"""`python -m forge3d` — the same entry point as the `forge3d` console script.

Without this, `python -m forge3d` failed with "No module named forge3d.__main__",
which is the first thing someone tries when the console script is not on PATH.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
