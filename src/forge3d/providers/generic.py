"""The generic worker provider — forge3d's original hosted contract.

Kept first and kept working: existing deployments point `FORGE3D_HOSTED_URL` at
their own shim and expect `POST /generate` + `GET /jobs/{id}`. Nothing about the
provider work changes that.

    POST {url}/generate   -> {job_id} (async)  or  {asset_url} (sync)
    GET  {url}/jobs/{id}  -> {status, asset_url, error}
"""

from __future__ import annotations

import os

from .base import (
    SUCCEEDED,
    GenerationProvider,
    GenerationRequest,
    ProviderError,
    ProviderJob,
    normalise_status,
    request_with_retries,
)


class GenericWorkerProvider(GenerationProvider):
    name = "generic"
    verified = True          # this one *is* the contract, and is covered by tests
    docs_url = "https://github.com/chaudharyvivek1/forge3d/blob/main/examples/hosted_worker.py"
    key_env = "FORGE3D_HOSTED_KEY"
    notes = ("Your own worker implementing forge3d's documented contract. "
             "examples/hosted_worker.py is a working reference.")

    def base_url(self) -> str | None:
        return os.environ.get("FORGE3D_HOSTED_URL")

    def available(self) -> bool:
        return bool(self.base_url())

    def capabilities(self) -> dict:
        return {
            "text_to_3d": True, "image_to_3d": True, "textures": True,
            "cancellable": False, "outputs": ["glb"], "self_hosted": True,
        }

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def submit(self, request: GenerationRequest) -> ProviderJob:
        base = self.base_url()
        if not base:
            raise ProviderError("generic provider needs FORGE3D_HOSTED_URL")

        payload = {
            "prompt": request.prompt,
            "images": request.images,
            "format": request.output_format,
            "seed": request.seed,
            "art_style": request.art_style,
            "provider": os.environ.get("FORGE3D_HOSTED_PROVIDER", "generic"),
        }
        resp = request_with_retries(
            "POST", f"{base.rstrip('/')}/generate", attempts=3,
            json=payload, headers=self._headers(), timeout=60,
        )
        self._raise_for_status(resp, "submit")
        data = self._json(resp)

        return ProviderJob(
            provider=self.name,
            job_id=str(data.get("job_id") or ""),
            status=SUCCEEDED if data.get("asset_url") else normalise_status(data.get("status")),
            asset_url=data.get("asset_url"),
            raw=data,
        )

    def get_status(self, job_id: str) -> ProviderJob:
        base = self.base_url()
        if not base:
            raise ProviderError("generic provider needs FORGE3D_HOSTED_URL")
        resp = request_with_retries(
            "GET", f"{base.rstrip('/')}/jobs/{job_id}", attempts=3,
            headers=self._headers(), timeout=60,
        )
        self._raise_for_status(resp, "status")
        data = self._json(resp)
        return ProviderJob(
            provider=self.name,
            job_id=job_id,
            status=normalise_status(data.get("status")),
            progress=float(data.get("progress") or 0.0),
            asset_url=data.get("asset_url"),
            error=data.get("error"),
            raw=data,
        )

    def check(self) -> dict:
        base = self.base_url()
        if not base:
            return {"provider": self.name, "ok": False,
                    "detail": "FORGE3D_HOSTED_URL is not set"}
        try:
            resp = request_with_retries("GET", f"{base.rstrip('/')}/health",
                                        attempts=1, timeout=10)
            return {"provider": self.name, "ok": resp.status_code < 400,
                    "detail": f"HTTP {resp.status_code} from /health"}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "ok": False, "detail": str(exc)[:200]}
