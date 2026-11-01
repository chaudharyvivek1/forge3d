"""A tiny durable job store.

3D generation is slow — a hosted text-to-3D call can take minutes, and an
OpenSCAD CSG rebuild on a complex part can take tens of seconds. Blocking an
agent's tool call on that is how you blow a context window or a timeout, so
every generation goes through a job record the agent can poll.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"


@dataclass
class Job:
    id: str
    backend: str
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    spec: dict = field(default_factory=dict)
    output: str | None = None
    error: str | None = None
    log: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def create(self, backend: str, spec: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], backend=backend, spec=spec)
        self.save(job)
        return job

    def save(self, job: Job) -> Job:
        job.updated_at = time.time()
        self._path(job.id).write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
        return job

    def get(self, job_id: str) -> Job | None:
        p = self._path(job_id)
        if not p.exists():
            return None
        return Job(**json.loads(p.read_text(encoding="utf-8")))

    def list(self, limit: int = 20) -> list[Job]:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[:limit]:
            try:
                out.append(Job(**json.loads(f.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def finish(self, job: Job, output: str | None, log: str = "", meta: dict | None = None) -> Job:
        job.status = SUCCEEDED if output else FAILED
        job.output = output
        job.log = log[-8000:]
        job.meta = meta or {}
        return self.save(job)

    def fail(self, job: Job, error: str) -> Job:
        job.status = FAILED
        job.error = error
        return self.save(job)
