"""Provider discovery and selection.

Selection order, and the reason for it:

1. an explicit name (`FORGE3D_PROVIDER`, or `provider=` on the call)
2. otherwise the first *available* commercial provider, in a fixed order
3. otherwise the generic worker, so the error message can name the env var that
   is actually missing rather than saying "no provider configured"
"""

from __future__ import annotations

import os

from .base import GenerationProvider, ProviderError
from .generic import GenericWorkerProvider
from .meshy import MeshyProvider
from .rodin import RodinProvider
from .tripo import TripoProvider

# Order matters: it is the auto-selection preference when several keys are set.
_CLASSES: tuple[type[GenerationProvider], ...] = (
    MeshyProvider,
    TripoProvider,
    RodinProvider,
    GenericWorkerProvider,
)

_REGISTRY: dict[str, GenerationProvider] = {cls.name: cls() for cls in _CLASSES}


def register(provider: GenerationProvider) -> None:
    """Add or replace a provider (used by tests and by downstream projects)."""
    _REGISTRY[provider.name] = provider


def get(name: str) -> GenerationProvider:
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise KeyError(f"unknown provider {name!r}; choose from {sorted(_REGISTRY)}")
    return _REGISTRY[key]


def names() -> list[str]:
    return list(_REGISTRY)


def list_providers() -> list[dict]:
    return [p.describe() for p in _REGISTRY.values()]


def available() -> list[GenerationProvider]:
    return [p for p in _REGISTRY.values() if p.available()]


def resolve(preferred: str | None = None) -> GenerationProvider:
    """Pick a provider, or explain precisely why none can be used."""
    explicit = (preferred or os.environ.get("FORGE3D_PROVIDER") or "").strip().lower()
    if explicit and explicit not in ("auto", ""):
        return get(explicit)

    for provider in _REGISTRY.values():
        if provider.name != "generic" and provider.available():
            return provider

    generic = _REGISTRY["generic"]
    if generic.available():
        return generic

    wanted = ", ".join(
        f"{p.name} ({p.key_env})" for p in _REGISTRY.values() if p.key_env
    )
    raise ProviderError(
        "no 3D provider is configured. Set one of: "
        f"{wanted}, or FORGE3D_HOSTED_URL for your own worker. "
        "Run `forge3d providers` to see what this machine has."
    )


def check_all(only: str | None = None) -> list[dict]:
    """Live connectivity check for each provider that has credentials."""
    targets = [get(only)] if only else list(_REGISTRY.values())
    results = []
    for provider in targets:
        if not provider.available():
            results.append({"provider": provider.name, "ok": False,
                            "detail": f"not configured ({provider.key_env})"})
            continue
        try:
            results.append(provider.check())
        except Exception as exc:  # noqa: BLE001 - a check must never raise
            results.append({"provider": provider.name, "ok": False,
                            "detail": str(exc)[:200]})
    return results
