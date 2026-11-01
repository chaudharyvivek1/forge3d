# Platforms

forge3d targets every platform a Python agent can plausibly run on. This page is
the honest state of that, per platform — including what does *not* work.

## Support matrix

| | Linux | macOS | Windows | Docker | Agent sandbox |
|---|---|---|---|---|---|
| `procedural` backend | ✅ | ✅ | ✅ | ✅ | ✅ |
| `openscad` backend | ✅ apt/snap | ✅ brew | ✅ winget | ⚠️ extend image | ⚠️ usually absent |
| `blender` backend | ✅ snap/apt | ✅ cask | ✅ winget | ⚠️ extend image | ❌ |
| `cadquery` backend | ✅ pip | ✅ pip | ⚠️ pip, needs OCCT wheels | ✅ pip | ⚠️ heavy |
| `hosted` backend | ✅ | ✅ | ✅ | ✅ | ✅ |
| CPU preview renders | ✅ | ✅ | ✅ | ✅ | ✅ |
| Single-file viewer | ✅ | ✅ | ✅ | ✅ | ✅ |
| MCP over stdio | ✅ | ✅ | ✅ | ✅ | ✅ |
| MCP over HTTP | ✅ | ✅ | ✅ | ✅ | ⚠️ port policy |
| Unicode model names | ✅ | ✅ | ✅ | ✅ | ✅ |
| Unicode paths | ✅ | ✅ | ⚠️ long paths | ✅ | ✅ |

Legend: ✅ works · ⚠️ works with a caveat noted below · ❌ not practical

## Platform notes

### Windows

- **Binaries are not on PATH.** The OpenSCAD installer and the Blender installer
  both default to *not* adding themselves to PATH. forge3d walks the usual
  install locations (`Program Files\OpenSCAD\openscad.exe`,
  `Program Files\Blender Foundation\Blender <ver>\blender.exe`,
  `%LOCALAPPDATA%\Programs\...`). If it still misses yours, set the path
  explicitly:

  ```powershell
  $env:FORGE3D_OPENSCAD = "C:\Program Files\OpenSCAD\openscad.exe"
  $env:FORGE3D_BLENDER  = "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
  ```

- **OpenSCAD ships two executables.** `openscad.exe` is the GUI subsystem build;
  `openscad.com` is the console build that returns output on stdout. forge3d
  prefers whichever it finds and both work, but `openscad.com` gives better logs.

- **Reserved filenames.** `con`, `prn`, `aux`, `nul`, `com1`–`com9`, `lpt1`–`lpt9`
  are illegal as file *or directory* names regardless of extension. A model
  called "aux" would fail to create its output directory on Windows only.
  `ModelSpec` now suffixes these (`aux-model`). This is the classic bug that
  ships because CI only ever ran on Linux.

- **Long paths.** Paths over 260 characters need `LongPathsEnabled` in the
  registry, or a `\\?\` prefix. Keep `FORGE3D_WORKDIR` short — e.g.
  `C:\f3d` rather than a deep project path.

- **Subprocess encoding.** Windows defaults `text=True` to cp1252, so a
  non-ASCII character in a compiler's error message raises
  `UnicodeDecodeError`. All subprocess calls now force UTF-8 with
  `errors="replace"`.

- **Console windows.** Spawning Blender or OpenSCAD from a GUI host would flash
  a console. forge3d passes `CREATE_NO_WINDOW` on Windows.

### macOS

- **Blender lives inside the app bundle**, not on PATH:
  `/Applications/Blender.app/Contents/MacOS/Blender`. Discovered automatically.
- **Apple Silicon vs Intel.** Both work; `cadquery` wheels exist for both on
  recent Pythons, but check `pip install cadquery` output if it tries to build
  from source.
- Homebrew casks install to `/Applications`, which is covered by discovery.

### Linux

- `apt install openscad` gives `/usr/bin/openscad`; snap and Flatpak paths
  (`/snap/bin/...`, `/var/lib/flatpak/exports/bin/...`) are also searched.
- Blender via snap is a confined package: it can only read/write inside
  `$HOME` (plus removable media). If your `FORGE3D_WORKDIR` is outside `$HOME`,
  the snap will fail to write the export. Use the apt build, the tarball, or
  keep the workdir under `$HOME`.

### Docker

The image ships the procedural backend only — deliberately. Blender alone is
about 1 GB and most deployments want a pinned version. Extend it:

```dockerfile
FROM forge3d:latest
USER root
RUN apt-get update && apt-get install -y openscad && rm -rf /var/lib/apt/lists/*
```

Mount a volume at `/work` (the default `FORGE3D_WORKDIR`) so generated models
survive the container.

### Agent sandboxes

Most hosted agent runtimes give you a CPU-only Linux container with a
restricted network and no GPU. That is exactly the environment forge3d is
designed for:

- rendering is CPU-only matplotlib — no OpenGL, no `pyglet`, no GPU;
- the procedural backend needs no external binary;
- the `hosted` backend needs outbound HTTPS to your text-to-3D worker;
- Blender will almost never be present, and `doctor` says so plainly rather
  than letting the agent guess.

## Timeouts and isolation

`FORGE3D_CODE_TIMEOUT` (default 120 s) bounds agent-written model code, and
`FORGE3D_EXEC_MODE` decides where that code runs.

| Mode | Cancellation | rlimits | Startup | Use when |
|---|---|---|---|---|
| `inprocess` | none — the work is abandoned and keeps running | no | instant | local stdio server, trusted agent |
| `subprocess` | **real** — the parent kills the child | `RLIMIT_CPU`, optional `RLIMIT_AS`, `RLIMIT_CORE=0` | ~1 s | anything reachable by someone else |
| `auto` (default) | per mode above | | | inprocess for stdio, subprocess for HTTP |

The in-process timeout is thread-based, not `signal`-based, because `signal.alarm`
does not exist on Windows and raises `ValueError: signal only works in main
thread` everywhere else — and MCP servers run synchronous tools in a threadpool.
Python cannot kill a thread, so `inprocess` can only abandon the work; that is
the honest limitation the `subprocess` mode exists to remove.

On Windows, `subprocess` mode still gives real cancellation (the parent kills the
child) but not rlimits, because the `resource` module is POSIX-only. Run the
container if you need memory ceilings there.

See [security.md](security.md) for what isolation does and does not buy you.

## Startup time

First import is the slowest part of any run: `trimesh` pulls in `scipy.spatial`,
which dominates a cold start (measured at ~13 s on a slow container disk with no
bytecode cache; roughly 1.5–3 s on a normal laptop with a warm cache).

That matters for MCP clients, which spawn the server and wait for the
`initialize` handshake before they will talk to it. Give a new server ~30 s on
first launch. Subsequent launches are faster once `__pycache__` exists.

If your client times out at startup, the fix is on the client side (raise its
spawn timeout) — nothing in forge3d is hanging; it is importing a geometry
library.

## Verifying your machine

```bash
forge3d doctor          # human-readable, with install hints
forge3d doctor --quiet  # JSON only
```

Or ask the agent: the `doctor` MCP tool returns the same report, including
copy-pasteable install commands for the current OS. It reports whether an env
var is *set*, never its value — so an API key in `FORGE3D_HOSTED_KEY` is not
echoed back.
