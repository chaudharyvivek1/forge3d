# Contributing

Short version: keep the layering, keep the returns small, and add a test that
would have caught the bug you just fixed.

## Setup

```bash
git clone https://github.com/chaudharyvivek1/forge3d.git
cd forge3d
pip install -e ".[render,dev]"
pytest -q
```

Optional, for the engine tests:

```bash
sudo apt install openscad blender     # Linux
pip install cadquery fast-simplification
```

Tests that need a binary skip themselves when it is missing, so a bare machine
gives a clean run. Do not make them fail instead — a red CI on a machine without
Blender teaches people to ignore CI.

## Rules

1. **No geometry in return values.** Tools return paths, counts, verdicts and
   hints. If you need to describe a mesh, extend `context.model_card`.
2. **Every failure gets a `hint`.** The hint names the next action, in the
   imperative: "call repair_model with fill_holes=true".
3. **New backends implement `backends.base.Backend`** and register in
   `registry.ALL`. Nothing downstream should need to know the engine exists.
4. **New validation checks carry a severity.** `error` blocks delivery, `warn` is
   a judgement call. Be conservative about `error` — a false blocker is worse
   than a missed warning.
5. **No network in tests.** The hosted backend is tested for availability logic
   only; never call a real service from CI.
6. **Keep `resolve_auto` a pure function.** It is routing logic, not a prompt.

## Adding a backend in five steps

```python
# src/forge3d/backends/mybinary.py
from .base import Backend, BackendResult, missing_binary
import shutil, subprocess
from pathlib import Path

class MyBinaryBackend(Backend):
    name = "mybinary"
    kind = "local"
    requires = ("mybinary",)
    capabilities = {"input": ["source"], "best_for": ["..."], "weak_for": ["..."]}

    def available(self) -> bool:
        return shutil.which("mybinary") is not None

    def generate(self, spec, out_dir, source=None, **kwargs) -> BackendResult:
        if not self.available():
            return missing_binary("mybinary")
        ...
```

Then add it to `ALL` in `registry.py`, add a case to `resolve_auto` if it should
be auto-selected, and add a skip-marked test in `tests/test_backends.py`.

## Style

`ruff check src tests` must pass. Line length 100. Type hints on public
functions. Docstrings explain *why* a thing exists, not what the code does.

## Commit messages

`area: what changed` — e.g. `validate: flag unit mix-ups in print profile`.
