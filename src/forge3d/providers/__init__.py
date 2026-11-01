"""Text-to-3D and image-to-3D provider adapters.

    from forge3d.providers import registry, GenerationRequest
    provider = registry.resolve()
    job = provider.submit(GenerationRequest(prompt="a ceramic teapot"))
    while not job.done:
        job = provider.get_status(job.job_id)

Each adapter states whether it has been verified against a live account; only the
generic worker has. `registry.check_all()` settles the rest with a real request.
"""

from . import registry
from .base import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    GenerationProvider,
    GenerationRequest,
    ProviderError,
    ProviderJob,
    normalise_status,
)
from .generic import GenericWorkerProvider
from .meshy import MeshyProvider
from .rodin import RodinProvider
from .tripo import TripoProvider

__all__ = [
    "GenerationProvider", "GenerationRequest", "ProviderJob", "ProviderError",
    "normalise_status", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED",
    "GenericWorkerProvider", "MeshyProvider", "TripoProvider", "RodinProvider",
    "registry",
]
