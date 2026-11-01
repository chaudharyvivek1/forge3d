"""Tripo adapter.

Written from Tripo's public OpenAPI documentation. **Not verified** against a
live account in this repo.

Endpoints used:
    POST /v2/openapi/task          {type, prompt | file}
    GET  /v2/openapi/task/{id}     {code, data: {status, progress, output}}
    POST /v2/openapi/task/{id}/cancel   (best effort)

Tripo wraps everything in `{code, data}`; code != 0 is an application error even
when the HTTP status is 200, which is the single easiest thing to get wrong here.
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


class TripoProvider(GenerationProvider):
    name = "tripo"
    verified = False
    docs_url = "https://platform.tripo3d.ai/docs/generation"
    key_env = "FORGE3D_TRIPO_KEY"
    notes = ("Text and image to 3D. Responses carry an application-level `code`, "
             "so HTTP 200 can still be a failure — handled explicitly.")

    def base_url(self) -> str:
        return os.environ.get("FORGE3D_TRIPO_BASE", "https://api.tripo3d.ai").rstrip("/")

    def capabilities(self) -> dict:
        return {
            "text_to_3d": True, "image_to_3d": True, "textures": True,
            "cancellable": True, "outputs": ["glb", "usdz", "fbx", "obj"],
            "multiview": True,
        }

    def estimate(self, request: GenerationRequest) -> dict:
        return {"credits": 20, "eta_seconds": 90, "currency_cost": None,
                "note": "credit cost depends on model version and texture settings"}

    def _headers(self) -> dict:
        key = self.api_key()
        if not key:
            raise ProviderError("tripo needs FORGE3D_TRIPO_KEY")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _unwrap(self, resp, what: str) -> dict:
        self._raise_for_status(resp, what)
        data = self._json(resp)
        code = data.get("code", 0)
        if code not in (0, None):
            raise ProviderError(
                f"tripo: {what} returned code {code}: "
                f"{data.get('message') or data.get('msg') or data}"
            )
        return data.get("data") or {}

    def submit(self, request: GenerationRequest) -> ProviderJob:
        if request.images:
            payload = {
                "type": "image_to_model",
                "file": {"type": "url", "url": request.images[0]},
            }
        else:
            payload = {"type": "text_to_model", "prompt": request.prompt}
            if request.negative_prompt:
                payload["negative_prompt"] = request.negative_prompt

        resp = self.submit_request(f"{self.base_url()}/v2/openapi/task", payload,
                                    idempotency_key=request.extra.get("idempotency_key"))
        data = self._unwrap(resp, "submit")
        job_id = data.get("task_id") or data.get("id")
        if not job_id:
            raise ProviderError(f"tripo: no task_id in response: {str(data)[:200]}")
        return ProviderJob(provider=self.name, job_id=str(job_id), raw=data)

    def get_status(self, job_id: str) -> ProviderJob:
        resp = request_with_retries("GET", f"{self.base_url()}/v2/openapi/task/{job_id}",
                                    headers=self._headers(), timeout=60)
        data = self._unwrap(resp, "status")
        output = data.get("output") or {}
        # Tripo reports 0-100. The earlier `progress / 100 if progress > 1` guard
        # turned 1% into 100% — one percent is exactly the value it got wrong.
        progress = min(1.0, max(0.0, float(data.get("progress") or 0.0) / 100.0))
        return ProviderJob(
            provider=self.name,
            job_id=job_id,
            status=normalise_status(data.get("status")),
            progress=progress,
            asset_url=output.get("pbr_model") or output.get("model"),
            error=data.get("error") or None,
            raw=data,
        )

    def cancel(self, job_id: str) -> bool:
        try:
            resp = request_with_retries(
                "POST", f"{self.base_url()}/v2/openapi/task/{job_id}/cancel",
                attempts=1, headers=self._headers(), timeout=30)
            return resp.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    def check(self) -> dict:
        if not self.api_key():
            return {"provider": self.name, "ok": False,
                    "detail": "FORGE3D_TRIPO_KEY is not set"}
        try:
            resp = request_with_retries("GET", f"{self.base_url()}/v2/openapi/task",
                                        attempts=1, headers=self._headers(), timeout=20)
            return {"provider": self.name, "ok": resp.status_code < 400,
                    "detail": f"HTTP {resp.status_code} listing tasks"}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "ok": False, "detail": str(exc)[:200]}
