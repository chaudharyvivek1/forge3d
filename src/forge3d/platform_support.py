"""Cross-platform plumbing: binary discovery, safe subprocess, safe timeouts.

Everything OS-specific in forge3d lives here, so no other module needs to know
whether it is running on Windows, macOS, Linux, in a container, or inside a
sandboxed agent runtime. Three problems this exists to solve, all of which were
real bugs before it existed:

1. **`signal.alarm` does not exist on Windows, and cannot be used from a worker
   thread on any OS.** MCP servers run synchronous tools in a threadpool, so the
   procedural backend's timeout crashed in the exact environment it was built
   for. `safe_timeout` uses a thread instead: portable, thread-safe, and honest
   about the fact that it abandons rather than kills runaway code.

2. **Binaries are not on PATH on Windows or macOS.** Blender lives inside
   `Blender.app/Contents/MacOS/`, OpenSCAD ships as both `openscad.exe` and
   `openscad.com`, and neither installs to PATH by default. Discovery walks a
   candidate list per OS, and an env var always wins.

3. **`text=True` without `encoding=` uses the platform default** (cp1252 on
   Windows, and worse under some locales). Any non-ASCII character in a
   compiler's error output then raises `UnicodeDecodeError` inside a subprocess
   call — a failure that only appears for non-English users.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX_OR_MACOS = not IS_WINDOWS
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Windows-only: stop console windows flashing up when a GUI host spawns us.
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


class TimeoutExceeded(Exception):
    """Raised by `safe_timeout` when the work outlives its budget."""


class ToolNotFound(RuntimeError):
    """A required external binary could not be located."""


def _limit_preexec() -> None:  # pragma: no cover - runs in the child
    """Apply rlimits in the forked child, before exec.

    POSIX only. `preexec_fn` is documented as unsafe in a threaded program, and
    the alternative (a wrapper process) does not help because the limits have to
    be set before exec anyway. Kept as narrow as possible for that reason.
    """
    import resource

    def _set(which, value):
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (value, value))

    cpu = int(os.environ.get("FORGE3D_CODE_TIMEOUT", "120") or 120)
    _set(resource.RLIMIT_CPU, max(1, cpu) + 1)
    _set(resource.RLIMIT_CORE, 0)
    _set(resource.RLIMIT_FSIZE, 512 * 1024 * 1024)
    with contextlib.suppress(Exception):
        _set(resource.RLIMIT_NOFILE, 256)
    mem_mb = int(os.environ.get("FORGE3D_MEM_LIMIT_MB", "0") or 0)
    if mem_mb > 0:
        _set(resource.RLIMIT_AS, mem_mb * 1024 * 1024)


@dataclass
class RunResult:
    ok: bool
    returncode: int
    output: str
    timed_out: bool = False
    limits_applied: bool = False
    command: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "output": self.output[-4000:],
        }


# ---------------------------------------------------------------------------
# binary discovery
# ---------------------------------------------------------------------------
def _windows_candidates(name: str) -> list[str]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("PROGRAMW6432", ""),
    ]
    out: list[str] = []
    for root in filter(None, roots):
        base = Path(root)
        for exe in (f"{name}.exe", f"{name}.com", name):
            out += [
                str(base / name / exe),
                str(base / f"{name} Foundation" / exe),
                str(base / "Programs" / name / exe),
            ]
    # versioned Blender installs: "Blender Foundation/Blender 4.2/blender.exe"
    foundation = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / f"{name} Foundation"
    if foundation.is_dir():
        for child in sorted(foundation.iterdir(), reverse=True):
            for exe in (f"{name}.exe", name):
                out.append(str(child / exe))
    return out


def _macos_candidates(name: str) -> list[str]:
    app_names = {"blender": "Blender", "openscad": "OpenSCAD"}.get(name, name.capitalize())
    homes = [Path("/Applications"), Path.home() / "Applications"]
    out = []
    for home in homes:
        out += [
            str(home / f"{app_names}.app" / "Contents" / "MacOS" / app_names),
            str(home / f"{app_names}.app" / "Contents" / "MacOS" / name),
        ]
    return out


def _linux_candidates(name: str) -> list[str]:
    return [
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/snap/bin/{name}",
        f"/var/lib/flatpak/exports/bin/org.{name}.{name.capitalize()}",
        str(Path.home() / ".local" / "bin" / name),
    ]


def find_binary(name: str, env_var: str | None = None, extra: list[str] | None = None) -> str | None:
    """Locate an executable, preferring an explicit env var over any guess.

    Returns the absolute path, or None. Never raises — availability is a fact to
    report, not an error to throw.
    """
    env_var = env_var or f"FORGE3D_{name.upper()}"
    override = os.environ.get(env_var)
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return str(p)

    for candidate in (name, f"{name}.exe", f"{name}.com") if IS_WINDOWS else (name,):
        found = shutil.which(candidate)
        if found:
            return found

    candidates: list[str] = list(extra or [])
    if IS_WINDOWS:
        candidates += _windows_candidates(name)
    elif IS_MACOS:
        candidates += _macos_candidates(name)
    else:
        candidates += _linux_candidates(name)

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def install_hint(name: str) -> str:
    """A copy-pasteable install command for this OS. Shown by `doctor`."""
    hints = {
        "openscad": {
            "win": "winget install OpenSCAD.OpenSCAD   (or https://openscad.org/downloads.html)",
            "mac": "brew install --cask openscad",
            "linux": "sudo apt install openscad   # or: sudo snap install openscad",
        },
        "blender": {
            "win": "winget install BlenderFoundation.Blender",
            "mac": "brew install --cask blender",
            "linux": "sudo snap install blender --classic   # or: sudo apt install blender",
        },
    }
    key = "win" if IS_WINDOWS else "mac" if IS_MACOS else "linux"
    return hints.get(name, {}).get(key, f"install {name} and put it on PATH, or set FORGE3D_{name.upper()}")


# ---------------------------------------------------------------------------
# subprocess
# ---------------------------------------------------------------------------
# Child environment policy: keep what a process needs to *run*, drop anything
# that looks like a credential. Deny wins over allow, so a variable named
# FORGE3D_HOSTED_KEY or AWS_SECRET_ACCESS_KEY never reaches agent-written code
# even if some future allowlist mentions its prefix.
_ENV_DENY = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE|SESSION|COOKIE)",
    re.IGNORECASE,
)

_ENV_KEEP_EXACT = frozenset({
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "TMPDIR", "TEMP", "TMP", "TERM", "VIRTUAL_ENV", "CONDA_PREFIX",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
})

# Prefixes that are never secrets and are load-bearing for the interpreter.
# PYTHONUSERBASE in particular: `pip install --user` and several distro builds
# put the package there, and dropping it made the child unable to import forge3d
# at all — hardening that breaks the product is not hardening.
_ENV_KEEP_PREFIX = ("PYTHON", "LC_", "XDG_")


def child_env(extra: dict | None = None) -> dict[str, str]:
    """A minimal, UTF-8-forced environment for child processes.

    `PYTHONIOENCODING`/`PYTHONUTF8` are not cosmetic: on Windows the default
    console encoding is cp1252, so a Python child that prints a Devanagari or CJK
    string dies with UnicodeEncodeError. The parent decoding UTF-8 is only half
    the fix — the child has to *emit* UTF-8 too. That exact bug failed the
    Windows CI job.

    Pass `extra` for values the child genuinely needs (e.g. the code timeout for
    its own CPU rlimit); they are merged last and override everything.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if _ENV_DENY.search(key):
            continue
        if key in _ENV_KEEP_EXACT or key.startswith(_ENV_KEEP_PREFIX):
            env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra:
        env.update(extra)
    return env


def run(cmd: list[str], timeout: int = 300, cwd: str | Path | None = None,
        stdin_text: str | None = None, env: dict | None = None,
        limits: bool = False) -> RunResult:
    """Run a command with UTF-8 I/O, a scrubbed environment and a hard timeout.

    `encoding="utf-8"` with `errors="replace"` is deliberate: without it, a
    single non-ASCII character in a tool's error message raises
    UnicodeDecodeError on Windows and hides the real failure.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            input=stdin_text,
            env=child_env(env),
            creationflags=_CREATE_NO_WINDOW,
            # rlimits can only be set before exec, so they go in the forked child.
            preexec_fn=(_limit_preexec if (limits and IS_LINUX_OR_MACOS) else None),
        )
    except subprocess.TimeoutExpired as exc:
        partial = ""
        for stream in (exc.stdout, exc.stderr):
            if stream:
                partial += stream if isinstance(stream, str) else stream.decode("utf-8", "replace")
        return RunResult(ok=False, returncode=-1, output=partial,
                         timed_out=True, command=[str(c) for c in cmd],
                         limits_applied=bool(limits and IS_LINUX_OR_MACOS))
    except FileNotFoundError:
        return RunResult(ok=False, returncode=127,
                         output=f"executable not found: {cmd[0]}", command=[str(c) for c in cmd])
    except OSError as exc:
        return RunResult(ok=False, returncode=126, output=f"could not run {cmd[0]}: {exc}",
                         command=[str(c) for c in cmd])

    output = (proc.stdout or "") + (proc.stderr or "")
    return RunResult(ok=proc.returncode == 0, returncode=proc.returncode,
                     output=output, command=[str(c) for c in cmd],
                     limits_applied=bool(limits and IS_LINUX_OR_MACOS))


# ---------------------------------------------------------------------------
# timeouts that work in threads and on Windows
# ---------------------------------------------------------------------------
def safe_timeout(fn, seconds: float, what: str = "operation"):
    """Run `fn()` with a wall-clock budget, from any thread, on any OS.

    Uses a daemon thread rather than `signal.alarm`, which is unavailable on
    Windows and raises `ValueError: signal only works in main thread` everywhere
    else — and MCP servers execute tools in a threadpool, so the main-thread
    restriction made the previous implementation unusable in production.

    Honest limitation: Python cannot kill a thread. On timeout the work is
    *abandoned*, not stopped — it keeps burning CPU until it finishes. That is
    why code execution is gated behind FORGE3D_ALLOW_CODE_EXEC; use a process
    sandbox if you need real containment.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=target, name=f"forge3d-{what}", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutExceeded(f"{what} exceeded {seconds:g}s (abandoned, not killed)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------
def atomic_write(path: str | Path, data: str | bytes) -> Path:
    """Write via a temp file in the same directory, then `os.replace`.

    `os.replace` is atomic on POSIX and on Windows (unlike `os.rename`), so a
    reader never sees a half-written mesh or a truncated HTML page.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    # Suffix `.tmp`, never the target's: a temp file named `...json` matches the
    # glob that `JobStore.list()` scans, so a concurrent reader could try to parse
    # a half-written record.
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".forge3d-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def writable_dir(path: str | Path) -> bool:
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".forge3d-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def doctor(workdir: str | Path | None = None) -> dict:
    """Environment report: what works here, and exactly how to fix what doesn't.

    Exposed as the `doctor` MCP tool and `forge3d doctor`, because an agent that
    can diagnose its own runtime stops guessing at why a backend is missing.
    """
    from . import __version__

    packages = {}
    for mod, pip_name in [
        ("trimesh", "trimesh"), ("numpy", "numpy"), ("scipy", "scipy"),
        ("matplotlib", "matplotlib"), ("fastmcp", "fastmcp"), ("requests", "requests"),
        ("cadquery", "cadquery"), ("manifold3d", "manifold3d"),
        ("fast_simplification", "fast-simplification"), ("pycollada", "pycollada"),
    ]:
        try:
            imported = __import__(mod)
            packages[mod] = {"installed": True,
                             "version": getattr(imported, "__version__", "unknown")}
        except Exception:
            packages[mod] = {"installed": False, "install": f"pip install {pip_name}"}

    engines = {}
    for name in ("openscad", "blender"):
        path = find_binary(name)
        engines[name] = {
            "available": path is not None,
            "path": path,
            **({} if path else {"install": install_hint(name)}),
        }
    engines["procedural"] = {"available": packages["trimesh"]["installed"],
                             "note": "no external binary required"}
    engines["cadquery"] = {"available": packages["cadquery"]["installed"],
                           **({} if packages["cadquery"]["installed"]
                              else {"install": "pip install cadquery"})}
    hosted_url = os.environ.get("FORGE3D_HOSTED_URL")
    engines["hosted"] = {"available": bool(hosted_url),
                         **({} if hosted_url
                            else {"install": "set FORGE3D_HOSTED_URL to your text-to-3D worker"})}

    from .config import get_settings as _get_settings

    settings = _get_settings()
    _cmd, isolation = sandbox_command(["true"], workdir, settings.sandbox)

    report = {
        "forge3d": __version__,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "is_windows": IS_WINDOWS,
            "is_macos": IS_MACOS,
            "is_linux": IS_LINUX,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "cpu_count": os.cpu_count(),
        },
        "engines": engines,
        "sandbox": {
            "requested": settings.sandbox,
            "effective": isolation,
            "backends": sandbox_backends(),
            "note": ("OS-level isolation active" if isolation != "rlimit"
                     else "rlimits only — install bubblewrap for filesystem and network isolation"),
        },
        "packages": packages,
        "workdir": {
            "path": str(workdir) if workdir else None,
            "writable": writable_dir(workdir) if workdir else None,
        },
        "env": {
            k: ("set" if os.environ.get(k) else "unset")
            for k in ("FORGE3D_WORKDIR", "FORGE3D_BACKEND", "FORGE3D_OPENSCAD",
                      "FORGE3D_BLENDER", "FORGE3D_HOSTED_URL", "FORGE3D_THREE_URL",
                      "FORGE3D_ALLOW_CODE_EXEC")
        },
    }

    hints = []
    if not engines["openscad"]["available"]:
        hints.append(f"OpenSCAD missing — {install_hint('openscad')}")
    if not engines["blender"]["available"]:
        hints.append(f"Blender missing — {install_hint('blender')}")
    if not packages["matplotlib"]["installed"]:
        hints.append("matplotlib missing — previews will be skipped: pip install matplotlib")
    if not packages["scipy"]["installed"]:
        hints.append("scipy missing — body count and volume are unavailable: pip install scipy")
    if isolation == "rlimit" and not sandbox_backends()["bwrap"]:
        hints.append("bubblewrap not found — model code gets rlimits but no filesystem or "
                     "network isolation: install bwrap (apt install bubblewrap)")
    if IS_WINDOWS:
        hints.append("Windows: if a binary is installed but not found, set FORGE3D_OPENSCAD / "
                     "FORGE3D_BLENDER to its full .exe path")
    report["hints"] = hints
    report["ready"] = bool(engines["procedural"]["available"])
    return report


# ---------------------------------------------------------------------------
# OS-level sandboxing for model code
# ---------------------------------------------------------------------------
def sandbox_backends() -> dict:
    """Which isolation mechanisms exist on this machine."""
    return {
        "bwrap": shutil.which("bwrap"),
        "unshare": shutil.which("unshare") if IS_LINUX else None,
        "rlimit": True,
    }


def sandbox_command(cmd: list[str], workdir: str | Path | None = None,
                    mode: str = "auto") -> tuple[list[str], str]:
    """Wrap `cmd` in the strongest isolation available, and say which one was used.

    bwrap (bubblewrap) is the only mechanism used automatically: it is designed
    for unprivileged use, so it either works or it does not. `unshare --net`
    needs capabilities that containers usually withhold — attempting it by
    default would turn every generation into a permission error, so it is
    opt-in via FORGE3D_SANDBOX=unshare.

    Returns (command, label). The label goes into the job metadata, so an audit
    can tell what isolation actually ran rather than what was configured.
    """
    mode = (mode or "auto").lower()
    if mode in ("none", "off", "rlimit"):
        return list(cmd), "rlimit"

    bwrap = shutil.which("bwrap")
    if bwrap and workdir and mode in ("auto", "bwrap"):
        wd = str(Path(workdir).resolve())
        wrapped = [
            bwrap,
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--unshare-net", "--unshare-pid", "--unshare-uts", "--unshare-ipc",
            "--die-with-parent", "--new-session",
            "--bind", wd, wd,
            "--chdir", wd,
            "--",
        ] + list(cmd)
        return wrapped, "bwrap"

    if mode == "unshare" and IS_LINUX:
        unshare = shutil.which("unshare")
        if unshare:
            return [unshare, "--net", "--"] + list(cmd), "unshare-net"

    if mode in ("auto", "bwrap", "unshare"):
        return list(cmd), "rlimit"
    return list(cmd), "rlimit"


def sandbox_selftest(mode: str = "auto") -> dict:
    """Actually try the isolation instead of assuming it works.

    A sandbox that silently fails to apply is worse than none: it produces
    confident metadata about protection that is not there.
    """
    backends = sandbox_backends()
    cmd, label = sandbox_command([sys.executable, "-c", "print('ok')"], Path.cwd(), mode)
    if label == "rlimit":
        return {"requested": mode, "effective": "rlimit", "verified": True,
                "detail": "no OS-level sandbox available; rlimits only",
                "backends": backends}
    result = run(cmd, timeout=30)
    return {
        "requested": mode,
        "effective": label,
        "verified": bool(result.ok and "ok" in result.output),
        "detail": (result.output or "").strip()[-300:],
        "backends": backends,
    }
