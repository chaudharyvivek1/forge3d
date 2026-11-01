"""A tiny durable job store.

3D generation is slow — a hosted text-to-3D call can take minutes, and an
OpenSCAD CSG rebuild on a complex part can take tens of seconds. Blocking an
agent's tool call on that is how you blow a context window or a timeout, so
every generation goes through a job record the agent can poll.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .platform_support import atomic_write

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"

# One lock for the whole process, not one per store instance. Each `_store()`
# call builds a new JobStore, so a per-instance lock serialised nothing between
# concurrent tool calls — the comment claimed process-wide safety it did not have.
_PROCESS_LOCK = threading.Lock()


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
        # MCP servers call tools from a threadpool, so two jobs can be written
        # concurrently. The lock is module-level (see _PROCESS_LOCK) so it is
        # shared by every store instance; atomic replace means a reader never
        # sees a half-written record.
        self._lock = _PROCESS_LOCK

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def create(self, backend: str, spec: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], backend=backend, spec=spec)
        self.save(job)
        return job

    def save(self, job: Job) -> Job:
        job.updated_at = time.time()
        with self._lock:
            atomic_write(self._path(job.id), json.dumps(job.to_dict(), indent=2))
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

    def touch(self, job_id: str) -> None:
        """Refresh a job's timestamp without disturbing its status.

        A long generation writes nothing for minutes, so `recover_stale` would
        eventually mark a perfectly healthy job as interrupted. This is the
        heartbeat that prevents it.
        """
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            job.updated_at = time.time()
            atomic_write(self._path(job.id), json.dumps(job.to_dict(), indent=2))

    @contextlib.contextmanager
    def heartbeat(self, job_id: str, interval: float = 30.0):
        """Keep `job_id` fresh while a slow operation runs."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(interval):
                try:
                    self.touch(job_id)
                except Exception:  # noqa: BLE001 - a heartbeat must never break a job
                    return

        thread = threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    def recover_stale(self, max_age_seconds: int = 900) -> list[str]:
        """Mark jobs orphaned by a restart as failed instead of `running` forever.

        Without this, a process that dies mid-generation leaves a job that looks
        alive forever, and an agent polling `get_job` waits on something that will
        never finish. Called at the start of every generation.
        """
        recovered = []
        now = time.time()
        for job in self.list(limit=200):
            if job.status in (QUEUED, RUNNING) and (now - job.updated_at) > max_age_seconds:
                job.status = FAILED
                job.error = (f"interrupted: no progress for {int(now - job.updated_at)}s "
                             "(server restarted or crashed mid-generation)")
                self.save(job)
                recovered.append(job.id)
        return recovered
