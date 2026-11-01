"""Rodin (Hyper3D) adapter.

**Lowest confidence of the three adapters.** Rodin's public API is less
consistently documented than Meshy's or Tripo's, and the status call is a POST
with a subscription key rather than a GET by id. Written from public
documentation, not verified against a live account.

Endpoints used:
    POST /api/v1/rodin     {prompt, image_urls, tier}  -> {uuid} or {jobs: {uuids}}
    POST /api/v1/status    {subscription_key}          -> {jobs: [{uuid, status, ...}]}
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


class RodinProvider(GenerationProvider):
    name = "rodin"
    verified = False
    docs_url = "https://developer.hyper3d.com/"
    key_env = "FORGE3D_RODIN_KEY"
    notes = ("Text and image to 3D. Status polling is a POST keyed by subscription, "
             "not a GET by id — the least standard of the adapters. Verify with "
             "`forge3d providers --check` before depending on it.")

    def base_url(self) -> str:
        return os.environ.get("FORGE3D_RODIN_BASE", "https://api.hyper3d.com").rstrip("/")

    def capabilities(self) -> dict:
        return {
            "text_to_3d": True, "image_to_3d": True, "textures": True,
            "cancellable": False, "outputs": ["glb", "usdz", "fbx", "obj"],
            "tiers": ["Regular", "Sketch", "Detail"],
        }

    def estimate(self, request: GenerationRequest) -> dict:
        return {"credits": None, "eta_seconds": 180, "currency_cost": None,
                "note": "billing depends on the tier requested"}

    def _headers(self) -> dict:
        key = self.api_key()
        if not key:
            raise ProviderError("rodin needs FORGE3D_RODIN_KEY")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def submit(self, request: GenerationRequest) -> ProviderJob:
        payload: dict = {"tier": request.extra.get("tier", "Regular")}
        if request.prompt:
            payload["prompt"] = request.prompt
        if request.images:
            payload["image_urls"] = list(request.images)
        if not payload.get("prompt") and not payload.get("image_urls"):
            raise ProviderError("rodin needs a prompt or at least one image")

        resp = request_with_retries("POST", f"{self.base_url()}/api/v1/rodin",
                                    json=payload,
                                    headers=self._headers(), timeout=60)
        self._raise_for_status(resp, "submit")
        data = self._json(resp)

        job_id = data.get("uuid")
        if not job_id:
            jobs = data.get("jobs") or {}
            uuids = jobs.get("uuids") or []
            job_id = uuids[0] if uuids else None
        if not job_id:
            raise ProviderError(f"rodin: no uuid in response: {str(data)[:200]}")
        return ProviderJob(provider=self.name, job_id=str(job_id), raw=data)

    def get_status(self, job_id: str) -> ProviderJob:
        resp = request_with_retries("POST", f"{self.base_url()}/api/v1/status",
                                    json={"subscription_key": job_id},
                                    headers=self._headers(), timeout=60)
        self._raise_for_status(resp, "status")
        data = self._json(resp)

        jobs = data.get("jobs") or []
        entry = next((j for j in jobs if str(j.get("uuid")) == str(job_id)),
                     jobs[0] if jobs else {})
        if not entry:
            return ProviderJob(provider=self.name, job_id=job_id, raw=data)

        files = entry.get("file") or {}
        return ProviderJob(
            provider=self.name,
            job_id=job_id,
            status=normalise_status(entry.get("status")),
            progress=float(entry.get("progress") or 0.0) / 100.0,
            asset_url=files.get("glb") or entry.get("glb_url"),
            error=entry.get("error") or None,
            raw=entry,
        )

    def check(self) -> dict:
        if not self.api_key():
            return {"provider": self.name, "ok": False,
                    "detail": "FORGE3D_RODIN_KEY is not set"}
        try:
            resp = request_with_retries("POST", f"{self.base_url()}/api/v1/status",
                                        attempts=1, json={"subscription_key": "probe"},
                                        headers=self._headers(), timeout=20)
            # A 4xx here still proves auth reached the API; a 401/403 does not.
            ok = resp.status_code not in (401, 403)
            return {"provider": self.name, "ok": ok,
                    "detail": f"HTTP {resp.status_code} on a probe status call"}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "ok": False, "detail": str(exc)[:200]}
