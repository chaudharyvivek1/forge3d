"""Meshy adapter.

Written from Meshy's public OpenAPI documentation. **Not verified** against a
live account in this repo — see `check()` and `forge3d providers --check`.

Endpoints used:
    POST   /openapi/v2/text-to-3d          {mode, prompt, art_style, should_remesh}
    POST   /openapi/v1/image-to-3d         {image_url}
    GET    /openapi/v2/text-to-3d/{id}     {status, progress, model_urls}
    GET    /openapi/v1/image-to-3d/{id}
    DELETE /openapi/v2/text-to-3d/{id}
"""

from __future__ import annotations

import os

from .base import (
    GenerationProvider,
    GenerationRequest,
    ProviderError,
    ProviderJob,
    normalise_status,
    request_with_retries,
)


class MeshyProvider(GenerationProvider):
    name = "meshy"
    verified = False
    docs_url = "https://docs.meshy.ai/api-text-to-3d"
    key_env = "FORGE3D_MESHY_KEY"
    notes = ("Text and image to 3D with textures. Written from public docs; "
             "run `forge3d providers --check` with a key before relying on it.")

    def base_url(self) -> str:
        return os.environ.get("FORGE3D_MESHY_BASE", "https://api.meshy.ai").rstrip("/")

    def capabilities(self) -> dict:
        return {
            "text_to_3d": True, "image_to_3d": True, "textures": True,
            "cancellable": True, "outputs": ["glb", "fbx", "usdz", "obj"],
            "stages": ["preview", "refine"],
        }

    def estimate(self, request: GenerationRequest) -> dict:
        # Meshy bills in credits; a preview is cheaper than a refine pass.
        return {"credits": 5 if request.art_style else 20, "eta_seconds": 120,
                "currency_cost": None, "note": "credit cost depends on your plan"}

    def _headers(self) -> dict:
        key = self.api_key()
        if not key:
            raise ProviderError("meshy needs FORGE3D_MESHY_KEY")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _endpoint(self, request: GenerationRequest) -> tuple[str, str]:
        if request.images:
            return "/openapi/v1/image-to-3d", "image"
        return "/openapi/v2/text-to-3d", "text"

    def submit(self, request: GenerationRequest) -> ProviderJob:
        path, kind = self._endpoint(request)
        if kind == "image":
            payload = {"image_url": request.images[0]}
        else:
            payload = {
                "mode": "preview",
                "prompt": request.prompt,
                "art_style": request.art_style or "realistic",
                "should_remesh": True,
            }
        resp = self.submit_request(self.base_url() + path, payload)
        self._raise_for_status(resp, "submit")
        data = self._json(resp)

        job_id = data.get("result") or data.get("id") or data.get("task_id")
        if not job_id:
            raise ProviderError(f"meshy: no job id in response: {str(data)[:200]}")
        job = ProviderJob(provider=self.name, job_id=str(job_id), raw=data)
        job.raw["_kind"] = kind
        return job

    def get_status(self, job_id: str) -> ProviderJob:
        path = f"/openapi/v2/text-to-3d/{job_id}"
        resp = request_with_retries("GET", self.base_url() + path,
                                    headers=self._headers(), timeout=60)
        if resp.status_code == 404:
            path = f"/openapi/v1/image-to-3d/{job_id}"
            resp = request_with_retries("GET", self.base_url() + path,
                                        headers=self._headers(), timeout=60)
        self._raise_for_status(resp, "status")
        data = self._json(resp)

        urls = data.get("model_urls") or {}
        return ProviderJob(
            provider=self.name,
            job_id=job_id,
            status=normalise_status(data.get("status")),
            progress=float(data.get("progress") or 0.0) / 100.0,
            asset_url=urls.get("glb") or urls.get("obj"),
            error=data.get("task_error", {}).get("message") if isinstance(
                data.get("task_error"), dict) else None,
            raw=data,
        )

    def cancel(self, job_id: str) -> bool:
        try:
            resp = request_with_retries("DELETE",
                                        f"{self.base_url()}/openapi/v2/text-to-3d/{job_id}",
                                        attempts=1, headers=self._headers(), timeout=30)
            return resp.status_code < 400
        except Exception:  # noqa: BLE001 - cancellation is best effort
            return False

    def check(self) -> dict:
        if not self.api_key():
            return {"provider": self.name, "ok": False,
                    "detail": "FORGE3D_MESHY_KEY is not set"}
        try:
            resp = request_with_retries("GET", f"{self.base_url()}/openapi/v2/text-to-3d",
                                        attempts=1, headers=self._headers(), timeout=20)
            return {"provider": self.name, "ok": resp.status_code < 400,
                    "detail": f"HTTP {resp.status_code} listing tasks"}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "ok": False, "detail": str(exc)[:200]}
