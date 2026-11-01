"""Hosted backend — hand the prompt to a text-to-3D / image-to-3D service.

Provider APIs differ enough that pretending otherwise would be dishonest, so
this adapter speaks one small, documented contract and ships a preset map for
the common services. Point `FORGE3D_HOSTED_URL` at either:

  (a) a service that already implements forge3d's contract, or
  (b) a thin worker you write in front of Meshy / Tripo / Rodin / Hunyuan3D.

Contract (all JSON over HTTPS, bearer auth):

    POST {url}/generate
      { "prompt": str, "images": [url], "format": "glb", "seed": int|null,
        "art_style": str|null, "provider": str }
      -> 202 { "job_id": "abc" }         (async, poll below)
      -> 200 { "asset_url": "https://..." }   (sync, done immediately)

    GET {url}/jobs/{job_id}
      -> { "status": "queued|running|succeeded|failed",
           "asset_url": "https://...", "error": str|null }

See docs/backends.md for why this indirection exists and how to write the worker.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from ..config import get_settings
from ..spec import ModelSpec
from .base import Backend, BackendResult

PROVIDER_PRESETS = {
    "generic": {"art_style": None, "format": "glb"},
    "meshy": {"art_style": "realistic", "format": "glb"},
    "tripo": {"art_style": "person:person2cartoon", "format": "glb"},
    "rodin": {"art_style": None, "format": "glb"},
    "hunyuan3d": {"art_style": None, "format": "glb"},
    "local": {"art_style": None, "format": "glb"},
}


class HostedBackend(Backend):
    name = "hosted"
    kind = "external"
    requires: tuple[str, ...] = ("FORGE3D_HOSTED_URL",)
    capabilities = {
        "input": ["text_prompt", "reference_images"],
        "deterministic": False,
        "text_to_3d": True,
        "image_to_3d": True,
        "textures": True,
        "best_for": ["organic forms", "stylised characters", "props", "fast iteration from a prompt"],
        "weak_for": ["exact dimensions", "watertight print geometry", "repeatable output"],
        "caveat": "licence terms and commercial-use rights are the provider's, not forge3d's",
    }

    def available(self) -> bool:
        return bool(get_settings().hosted_url)

    def generate(self, spec: ModelSpec, out_dir: Path, source: str | None = None, **kwargs) -> BackendResult:
        settings = get_settings()
        if not settings.hosted_url:
            return BackendResult(
                ok=False,
                error="FORGE3D_HOSTED_URL is not set",
                log="Set it to your text-to-3D worker, or use backend='procedural'.",
            )

        base = settings.hosted_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if settings.hosted_key:
            headers["Authorization"] = f"Bearer {settings.hosted_key}"

        preset = PROVIDER_PRESETS.get(settings.hosted_provider, PROVIDER_PRESETS["generic"])
        payload = {
            "prompt": source or spec.prompt_for_backend(),
            "images": spec.references,
            "format": preset["format"],
            "seed": spec.seed,
            "art_style": kwargs.get("art_style", preset["art_style"]),
            "provider": settings.hosted_provider,
        }

        try:
            resp = requests.post(f"{base}/generate", json=payload, headers=headers, timeout=60)
        except requests.RequestException as exc:
            return BackendResult(ok=False, error=f"could not reach hosted backend: {exc}")

        if resp.status_code >= 400:
            return BackendResult(ok=False, error=f"hosted backend returned {resp.status_code}", log=resp.text[-2000:])

        data = resp.json()
        asset_url = data.get("asset_url")
        job_id = data.get("job_id")

        deadline = time.time() + settings.job_timeout
        while not asset_url and job_id:
            if time.time() > deadline:
                return BackendResult(ok=False, error=f"hosted job {job_id} exceeded {settings.job_timeout}s")
            time.sleep(max(2, int(kwargs.get("poll_interval", 5))))
            try:
                poll = requests.get(f"{base}/jobs/{job_id}", headers=headers, timeout=60).json()
            except (requests.RequestException, ValueError) as exc:
                return BackendResult(ok=False, error=f"polling failed: {exc}")
            status = poll.get("status")
            if status == "failed":
                return BackendResult(ok=False, error=poll.get("error") or "hosted generation failed")
            if status == "succeeded":
                asset_url = poll.get("asset_url")

        if not asset_url:
            return BackendResult(ok=False, error="hosted backend returned no asset", log=str(data)[:2000])

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(asset_url.split("?")[0]).suffix or ".glb"
        out_path = out_dir / f"{spec.name}{suffix}"
        try:
            with requests.get(asset_url, stream=True, timeout=300) as dl:
                dl.raise_for_status()
                with open(out_path, "wb") as fh:
                    fh.writelines(dl.iter_content(chunk_size=1 << 16))
        except (requests.RequestException, OSError) as exc:
            return BackendResult(ok=False, error=f"download failed: {exc}")

        return BackendResult(
            ok=True,
            path=str(out_path),
            meta={"engine": "hosted", "provider": settings.hosted_provider, "job_id": job_id},
            log="hosted generation complete; run validate_model before shipping — "
                "text-to-3D output is rarely watertight or to scale.",
        )
