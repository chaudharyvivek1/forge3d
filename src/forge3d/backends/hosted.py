"""Hosted backend — hand the prompt to a text-to-3D / image-to-3D provider.

This used to speak only forge3d's own worker contract, which meant every user had
to host a shim in front of Meshy or Tripo. It now delegates to the provider
registry: real adapters for Meshy, Tripo and Rodin, plus the original generic
worker, all behind one interface.

`FORGE3D_HOSTED_URL` still works exactly as before — the generic provider *is*
that contract — so existing deployments do not change.

    FORGE3D_PROVIDER=meshy  FORGE3D_MESHY_KEY=msy_...   forge3d create ...
    FORGE3D_HOSTED_URL=http://localhost:8100            forge3d create ...

Which providers this machine can actually use:

    forge3d providers            # list, with verified flags
    forge3d providers --check    # live connectivity + auth check
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from ..config import get_settings
from ..providers import registry
from ..providers.base import (
    SUCCEEDED,
    GenerationRequest,
    ProviderError,
    request_with_retries,
)
from ..spec import ModelSpec
from .base import Backend, BackendResult

# Kept as a module-level alias: existing tests and downstream code import it.
_request = request_with_retries


class HostedBackend(Backend):
    name = "hosted"
    kind = "external"
    requires: tuple[str, ...] = ("FORGE3D_HOSTED_URL or a provider key",)
    capabilities = {
        "input": ["text_prompt", "reference_images"],
        "deterministic": False,
        "text_to_3d": True,
        "image_to_3d": True,
        "textures": True,
        "providers": ["meshy", "tripo", "rodin", "generic"],
        "best_for": ["organic forms", "stylised characters", "props", "fast iteration from a prompt"],
        "weak_for": ["exact dimensions", "watertight print geometry", "repeatable output"],
        "caveat": "licence terms and commercial-use rights are the provider's, not forge3d's",
    }

    # -- availability ----------------------------------------------------
    def available(self) -> bool:
        return bool(registry.available())

    def describe(self) -> dict:
        info = super().describe()
        info["providers"] = registry.list_providers()
        return info

    def estimate(self, spec: ModelSpec) -> dict:
        """Cost and ETA from the selected provider, before anything is spent."""
        try:
            provider = registry.resolve(get_settings().provider)
        except ProviderError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "provider": provider.name,
            "verified": provider.verified,
            **provider.estimate(self._request_for(spec, None)),
        }

    # -- generation ------------------------------------------------------
    @staticmethod
    def _request_for(spec: ModelSpec, source: str | None, **kwargs) -> GenerationRequest:
        return GenerationRequest(
            prompt=source or spec.prompt_for_backend(),
            images=list(spec.references),
            art_style=kwargs.get("art_style"),
            seed=spec.seed,
            output_format=kwargs.get("output_format", "glb"),
            extra=dict(kwargs.get("provider_extra") or {}),
        )

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None,
                 **kwargs) -> BackendResult:
        settings = get_settings()
        try:
            provider = registry.resolve(settings.provider)
        except ProviderError as exc:
            return BackendResult(ok=False, error=str(exc),
                                 log="`forge3d providers` lists what is configured here.")

        request = self._request_for(spec, source, **kwargs)
        if not request.prompt and not request.images:
            return BackendResult(
                ok=False,
                error="nothing to send: give a prompt or at least one reference image",
            )

        try:
            job = provider.submit(request)
        except ProviderError as exc:
            return BackendResult(ok=False, error=str(exc))
        except requests.RequestException as exc:
            return BackendResult(ok=False, error=f"could not reach {provider.name}: {exc}")

        poll = max(2, int(kwargs.get("poll_interval", 5)))
        deadline = time.time() + settings.job_timeout
        while not job.done:
            if time.time() > deadline:
                return BackendResult(
                    ok=False,
                    error=f"{provider.name} job {job.job_id} exceeded {settings.job_timeout}s",
                    log=f"last status: {job.status}, progress {job.progress:.0%}",
                    meta={"provider": provider.name, "job_id": job.job_id},
                )
            time.sleep(poll)
            try:
                job = provider.get_status(job.job_id)
            except (ProviderError, requests.RequestException) as exc:
                return BackendResult(ok=False, error=f"polling failed: {exc}")

        if job.status != SUCCEEDED:
            return BackendResult(
                ok=False,
                error=f"{provider.name} generation {job.status}: {job.error or 'no detail'}",
                meta={"provider": provider.name, "job_id": job.job_id},
            )

        try:
            path = provider.download(job, Path(out_dir), spec.name)
        except (ProviderError, requests.RequestException, OSError) as exc:
            return BackendResult(ok=False, error=f"download failed: {exc}")

        log = (
            f"{provider.name} generation complete"
            + ("" if provider.verified else
               " (this adapter is unverified — run `forge3d providers --check` "
               "with a real key if anything looks off)")
            + ". Run validate_model before shipping: text-to-3D output is rarely "
              "watertight or to scale."
        )
        return BackendResult(
            ok=True,
            path=str(path),
            meta={
                "engine": "hosted",
                "provider": provider.name,
                "provider_verified": provider.verified,
                "job_id": job.job_id,
                "progress": job.progress,
            },
            log=log,
        )
