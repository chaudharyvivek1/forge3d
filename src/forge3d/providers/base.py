"""Provider contract: one shape for every text-to-3D / image-to-3D service.

`HostedBackend` spoke one generic worker contract. That was honest but limited:
every user had to write and host a shim in front of Meshy or Tripo. This package
adds real adapters behind a single interface, without breaking the generic
worker — `GenericWorkerProvider` is that same contract, so existing deployments
keep working.

The interface is deliberately small and job-shaped, because every one of these
services is asynchronous:

    submit()      -> a job handle
    get_status()  -> queued | running | succeeded | failed | cancelled
    download()    -> a file on disk
    cancel()      -> best effort, may be unsupported

**Honesty about verification.** `verified` on each provider means "exercised
against a live account by the maintainer". It is False for the commercial
adapters: they were written from public API documentation and their mapping logic
is covered by tests against recorded response shapes, but nobody has run them with
a real key in this repo. `check()` exists so you can settle it in one command:

    forge3d providers --check

A provider that has not been verified says so in `list_providers`, in the CLI and
in the error you get when it fails. That is the point: a silent "not integrated"
is worse than a loud "unverified".
"""

from __future__ import annotations

import json
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import requests

# Normalised lifecycle. Every provider maps its own vocabulary onto these.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
#: The request may or may not have created a job. Never treat this as failure
#: without checking the provider: retrying could buy a second generation.
UNCERTAIN = "uncertain"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

_STATUS_ALIASES = {
    # generic
    "queued": QUEUED, "pending": QUEUED, "waiting": QUEUED, "submitted": QUEUED,
    "running": RUNNING, "in_progress": RUNNING, "processing": RUNNING,
    "inprogress": RUNNING, "generating": RUNNING, "queuing": QUEUED,
    "succeeded": SUCCEEDED, "success": SUCCEEDED, "completed": SUCCEEDED,
    "complete": SUCCEEDED, "done": SUCCEEDED, "finished": SUCCEEDED,
    "failed": FAILED, "error": FAILED, "cancelled": CANCELLED,
    "canceled": CANCELLED, "expired": FAILED,
}


def normalise_status(raw: str | None) -> str:
    """Map a provider's own status word onto ours.

    Every service invents its own vocabulary (PENDING / IN_PROGRESS / success /
    finished…). Anything unrecognised becomes `running` rather than `failed` — an
    unknown word is not evidence of failure, and treating it as one would abort
    jobs that are actually fine.
    """
    if not raw:
        return RUNNING
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return _STATUS_ALIASES.get(key, RUNNING)


@dataclass
class GenerationRequest:
    prompt: str = ""
    images: list[str] = field(default_factory=list)
    art_style: str | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    output_format: str = "glb"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt, "images": list(self.images),
            "art_style": self.art_style, "negative_prompt": self.negative_prompt,
            "seed": self.seed, "output_format": self.output_format,
        }


@dataclass
class ProviderJob:
    provider: str
    job_id: str
    status: str = QUEUED
    progress: float = 0.0
    asset_url: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "job_id": self.job_id, "status": self.status,
            "progress": round(float(self.progress), 3),
            "asset_url": self.asset_url, "error": self.error,
        }


def request_with_retries(method: str, url: str, attempts: int | None = None,
                         idempotent: bool = True, **kwargs) -> requests.Response:
    """HTTP with bounded exponential backoff and jitter.

    Retries 5xx and connection errors; never 4xx — a bad request is the caller's
    fault, and retrying it burns the provider's quota and your money.

    `attempts=None` means "use FORGE3D_HOSTED_RETRIES" (default 3). The adapters
    used to hardcode 3, which silently ignored the documented setting.

    `idempotent=False` is for job-creation POSTs. There, a retry after an
    ambiguous timeout can buy a second generation, so only connection failures
    that happen *before* the request could have been accepted are retried — never
    a timeout, never a 5xx (which may mean "created, then failed to respond").
    """
    if attempts is None:
        try:
            attempts = int(os.environ.get("FORGE3D_HOSTED_RETRIES", "3"))
        except ValueError:
            attempts = 3
    attempts = max(1, int(attempts))
    delay = 1.0
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code >= 500:
                if not idempotent:
                    raise ProviderError(
                        f"{method} {url} returned {resp.status_code}; the request may "
                        f"already have been accepted — not retrying"
                    )
                raise requests.RequestException(f"server error {resp.status_code}")
            return resp
        except requests.RequestException as exc:
            last = exc
            if attempt == attempts - 1:
                break
            # Only a failure to *connect* is provably pre-acceptance. A read
            # timeout on a POST may mean the provider created the job already.
            if not idempotent and isinstance(
                exc, (requests.Timeout, requests.ConnectionError)
            ) and not isinstance(exc, requests.ConnectTimeout):
                break
            time.sleep(delay + random.uniform(0, delay * 0.4))
            delay = min(delay * 2, 30.0)
    raise last if last else requests.RequestException("request failed")


class ProviderError(RuntimeError):
    """A provider call failed in a way the caller should see verbatim."""


class AmbiguousSubmission(ProviderError):
    """A job-creation request timed out after it may have been accepted.

    The dangerous case: the provider created the job, the response was lost, and
    a blind retry creates a *second* paid job. Callers must reconcile with the
    provider rather than retry. `idempotency_key` is what to reconcile with.
    """

    def __init__(self, provider: str, idempotency_key: str, detail: str = "") -> None:
        self.provider = provider
        self.idempotency_key = idempotency_key
        super().__init__(
            f"{provider}: submission outcome unknown — the request may have been "
            f"accepted before the connection failed. Do NOT retry blindly; check the "
            f"provider dashboard for key {idempotency_key}. {detail}".strip()
        )


class GenerationProvider(ABC):
    """Base class. Subclasses fill in the four lifecycle methods."""

    name: str = "base"
    #: True only if the maintainer has run this against a live account.
    verified: bool = False
    docs_url: str = ""
    #: Environment variable holding the API key, if any.
    key_env: str | None = None
    #: What this provider is honest about.
    notes: str = ""

    # -- introspection ---------------------------------------------------
    def api_key(self) -> str | None:
        import os

        return os.environ.get(self.key_env) if self.key_env else None

    def available(self) -> bool:
        """Can this provider be used right now?"""
        return self.api_key() is not None

    def capabilities(self) -> dict:
        return {
            "text_to_3d": True,
            "image_to_3d": True,
            "textures": True,
            "cancellable": True,
            "outputs": ["glb"],
        }

    def estimate(self, request: GenerationRequest) -> dict:
        """Rough cost/time expectation, so a caller can budget before spending."""
        return {"credits": None, "eta_seconds": None, "currency_cost": None}

    def describe(self) -> dict:
        return {
            "name": self.name,
            "available": self.available(),
            "verified": self.verified,
            "docs_url": self.docs_url,
            "key_env": self.key_env,
            "capabilities": self.capabilities(),
            "notes": self.notes,
        }

    # -- lifecycle -------------------------------------------------------
    @abstractmethod
    def submit(self, request: GenerationRequest) -> ProviderJob:
        ...

    @abstractmethod
    def get_status(self, job_id: str) -> ProviderJob:
        ...

    def cancel(self, job_id: str) -> bool:
        """Best effort. Returns False when the provider has no cancel endpoint."""
        return False

    def download(self, job: ProviderJob, out_dir: Path, name: str) -> Path:
        """Fetch the finished asset. Shared by every HTTP-based provider."""
        if not job.asset_url:
            raise ProviderError(f"{self.name}: job {job.job_id} has no asset URL")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(job.asset_url.split("?")[0]).suffix or ".glb"
        target = out_dir / f"{name}{suffix}"
        limit = int(os.environ.get("FORGE3D_MAX_DOWNLOAD_MB", "512")) * 1024 * 1024
        written = 0
        with request_with_retries("GET", job.asset_url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(target, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > limit:
                        fh.close()
                        target.unlink(missing_ok=True)
                        raise ProviderError(
                            f"{self.name}: download exceeded "
                            f"{limit // (1024 * 1024)} MB (FORGE3D_MAX_DOWNLOAD_MB)"
                        )
                    fh.write(chunk)
        return target

    def check(self) -> dict:
        """Live connectivity/auth check. Override where the provider offers one."""
        return {
            "provider": self.name,
            "ok": False,
            "detail": "no live check implemented for this provider",
        }

    # -- helpers ---------------------------------------------------------
    def submit_request(self, url: str, payload: dict, timeout: int = 60,
                       attempts: int | None = None) -> requests.Response:
        """POST a job-creation request safely.

        Adds an `Idempotency-Key` header (providers that support it will dedupe),
        refuses to retry an ambiguous failure, and converts a timeout into
        `AmbiguousSubmission` so the caller can reconcile instead of double-paying.
        """
        import uuid

        key = uuid.uuid4().hex
        headers = dict(self._headers())
        headers["Idempotency-Key"] = key
        try:
            return request_with_retries(
                "POST", url, attempts=attempts, idempotent=False,
                json=payload, headers=headers, timeout=timeout,
            )
        except requests.Timeout as exc:
            raise AmbiguousSubmission(self.name, key, str(exc)) from exc
        except ProviderError:
            raise
        except requests.RequestException as exc:
            raise ProviderError(f"{self.name}: could not submit ({exc})") from exc

    def _json(self, resp: requests.Response) -> dict:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"{self.name}: response was not JSON (HTTP {resp.status_code}): "
                f"{resp.text[:200]}"
            ) from exc

    def _raise_for_status(self, resp: requests.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        raise ProviderError(
            f"{self.name}: {what} failed with HTTP {resp.status_code}: {resp.text[:300]}"
        )
