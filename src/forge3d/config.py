"""Runtime settings. Everything is env-overridable so the same repo works
locally, in CI and inside a hosted agent sandbox."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_bool_log() -> bool:
    return _env_bool("FORGE3D_LOG_JSON", False)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """Process-wide configuration.

    `workdir` is where generated meshes, previews and job records land. Keep it
    outside the repo (the default is a dot-directory) so nothing heavy is ever
    committed by accident.
    """

    workdir: Path = field(default_factory=lambda: Path(os.environ.get("FORGE3D_WORKDIR", ".forge3d")))
    default_backend: str = field(default_factory=lambda: os.environ.get("FORGE3D_BACKEND", "auto"))

    # Face-count ceilings per delivery target. Overridable because "low poly"
    # means something different to a mobile game and to a resin printer.
    max_faces_web: int = field(default_factory=lambda: _env_int("FORGE3D_MAX_FACES_WEB", 250_000))
    max_faces_game: int = field(default_factory=lambda: _env_int("FORGE3D_MAX_FACES_GAME", 60_000))
    max_faces_print: int = field(default_factory=lambda: _env_int("FORGE3D_MAX_FACES_PRINT", 2_000_000))

    # Hosted text-to-3D adapter (Meshy / Tripo / Rodin / a self-hosted worker).
    hosted_url: str | None = field(default_factory=lambda: os.environ.get("FORGE3D_HOSTED_URL"))
    hosted_key: str | None = field(default_factory=lambda: os.environ.get("FORGE3D_HOSTED_KEY"))
    hosted_provider: str = field(default_factory=lambda: os.environ.get("FORGE3D_HOSTED_PROVIDER", "generic"))
    # Which text-to-3D provider to use: auto | generic | meshy | tripo | rodin
    provider: str = field(default_factory=lambda: os.environ.get("FORGE3D_PROVIDER", "auto"))

    # Executing agent-written Python is the most capable backend, and the most
    # dangerous one. It is on by default because forge3d is normally run as a
    # local stdio server by a coding agent; turn it off for untrusted callers.
    allow_code_exec: bool = field(default_factory=lambda: _env_bool("FORGE3D_ALLOW_CODE_EXEC", True))
    code_timeout: int = field(default_factory=lambda: _env_int("FORGE3D_CODE_TIMEOUT", 120))
    job_timeout: int = field(default_factory=lambda: _env_int("FORGE3D_JOB_TIMEOUT", 900))

    # How agent-written model code runs.
    #   inprocess  — exec() in the server process. Fast, no isolation, and a
    #                timeout can only abandon the work (Python cannot kill a
    #                thread). Only safe for a stdio server you started yourself.
    #   subprocess — a child process with rlimits and a hard kill on timeout.
    #                Real cancellation, real memory ceiling. Slower to start.
    #   auto       — inprocess for stdio, subprocess when serving HTTP.
    exec_mode: str = field(default_factory=lambda: os.environ.get("FORGE3D_EXEC_MODE", "auto").lower())
    # Memory ceiling for subprocess mode, in MiB. 0 disables it. Not a substitute
    # for a container: it bounds address space, not every kind of allocation.
    mem_limit_mb: int = field(default_factory=lambda: _env_int("FORGE3D_MEM_LIMIT_MB", 0))

    # --- reliability -----------------------------------------------------
    # How many generations may run at once. 3D work is CPU-bound; letting ten
    # concurrent builds share four cores makes every one of them slow and can
    # trip client timeouts that look like server failures.
    max_concurrent: int = field(default_factory=lambda: _env_int("FORGE3D_MAX_CONCURRENT", 2))
    # How long a request may wait for a free slot before being told the server is
    # busy. A short queue absorbs bursts; an unbounded one hides overload and
    # turns into client timeouts that look like server failures.
    queue_wait: int = field(default_factory=lambda: _env_int("FORGE3D_QUEUE_WAIT", 30))
    # OS-level isolation for model code: auto | bwrap | unshare | rlimit | none
    sandbox: str = field(default_factory=lambda: os.environ.get("FORGE3D_SANDBOX", "auto").lower())
    # HTTP requests per minute, per token/IP. 0 disables the limiter.
    rate_limit: int = field(default_factory=lambda: _env_int("FORGE3D_RATE_LIMIT", 0))
    # Structured JSON logs on stderr, for log aggregators.
    log_json: bool = field(default_factory=_env_bool_log)
    # Attempts for hosted text-to-3D calls (1 = no retry).
    hosted_retries: int = field(default_factory=lambda: _env_int("FORGE3D_HOSTED_RETRIES", 3))

    def resolved_exec_mode(self) -> str:
        mode = self.exec_mode
        if mode in ("inprocess", "subprocess"):
            return mode
        return "subprocess" if os.environ.get("FORGE3D_TRANSPORT") == "http" else "inprocess"

    def ensure_workdir(self) -> Path:
        self.workdir.mkdir(parents=True, exist_ok=True)
        return self.workdir


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
