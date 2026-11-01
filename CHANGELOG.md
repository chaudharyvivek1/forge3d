# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/).

## [0.2.1] — 2026-11-01

Response to an external security and reliability review. Four of the five
findings were fixed in code rather than merely documented.

### Security

- **The HTTP transport bound `0.0.0.0` with no authentication.** Anyone who could
  reach the port could call every tool, including arbitrary model code. It now
  binds `127.0.0.1` by default, refuses a public bind without `FORGE3D_HTTP_TOKEN`
  (exit code 2 with an explanation), and requires
  `Authorization: Bearer <token>` compared with `hmac.compare_digest`. `/health`
  stays open for probes. `--allow-unauthenticated` is the explicit escape hatch.
- **HTTP mode now forces `subprocess` exec mode**, since a network caller is not
  assumed trustworthy.
- `docs/security.md` states the threat model and, just as importantly, what is
  *not* protected: no TLS, no rate limiting, no per-user identity, and subprocess
  isolation is a reliability boundary rather than a security one.

### Added

- **`FORGE3D_EXEC_MODE`** — `inprocess` (fast, no isolation), `subprocess`
  (killable child with rlimits), or `auto` (inprocess for stdio, subprocess for
  HTTP). New `sandbox_runner` module implements the child: `RLIMIT_CPU`,
  optional `RLIMIT_AS` via `FORGE3D_MEM_LIMIT_MB`, `RLIMIT_CORE=0`, and output
  trusted only as files on disk.
- **Real cancellation.** In subprocess mode a timed-out build is killed by the
  parent, answering the review's central objection to the thread-based timeout.
- `examples/hosted_worker.py` — a stdlib-only reference worker implementing the
  hosted contract, so the text-to-3D path is testable end to end and one real
  provider integration is a single function away.
- `forge3d[http]` extra (uvicorn).

### Fixed

- **Concurrent generations of the same model name overwrote each other.** Output
  now goes to `models/<name>/<job-id>/`, and the full path is returned.
- **Job records were written non-atomically**, and a process that died
  mid-generation left a job stuck in `running` forever. Writes are now atomic and
  guarded by a lock; `recover_stale()` marks orphaned jobs as `interrupted` at the
  start of each generation.
- Starlette middleware must be passed as `(cls, args, kwargs)` triples — the first
  implementation passed bare callables and failed at app construction. Caught by
  the live-server test.

### Tests

162 passing (was 135). New `tests/test_security.py` covers the bind policy, the
bearer middleware, a **live uvicorn server** rejecting unauthenticated requests,
runaway code actually being killed, job isolation under concurrency, and stale-job
recovery.

## [0.2.0] — 2026-11-01

Cross-platform hardening and a bug audit of every module. Seven real defects
fixed, each with a regression test in `tests/test_regressions.py`.

### Fixed (summary)

See the sections below for detail.

### Fixed

- **Procedural backend failed in a worker thread.** The code timeout used
  `signal.alarm`, which does not exist on Windows and raises `ValueError: signal
  only works in main thread` elsewhere — and MCP servers execute synchronous
  tools in a threadpool, so the default backend was broken in the environment it
  was built for. Timeouts are now thread-based (`platform_support.safe_timeout`).
- **`.gltf` export was impossible.** trimesh returns a dict of
  `filename -> payload` for multi-file formats, and that dict was passed to
  `write_bytes`, raising `TypeError: memoryview: a bytes-like object is required`.
  Sidecar buffers are now written next to the main file.
- **HTML/script injection through a model name.** A title containing `</script>`
  terminated the viewer's script block early — a genuine XSS vector when the name
  comes from user input. Titles are HTML-escaped and the inline JSON is
  unicode-escaped.
- **`embed=False` produced a broken page.** The generated HTML linked a relative
  mesh path that was never written. The mesh is now copied beside the page.
- **Non-Latin model names collapsed to `model`.** `[^a-zA-Z0-9]` stripping turned
  "कुर्सी" and "椅子" into "model", inviting silent collisions. Unicode letters,
  digits and combining marks (matras) are preserved.
- **Windows-illegal filenames were not avoided.** `con`, `prn`, `aux`, `nul`,
  `com1`–`com9`, `lpt1`–`lpt9` cannot be file or directory names on Windows;
  they are now suffixed.
- **Subprocess output was decoded with the platform default encoding.**
  `text=True` without `encoding=` uses cp1252 on Windows, so any non-ASCII
  character in a tool's error message raised `UnicodeDecodeError`.
- **`.dae` export raised a raw `ImportError`** instead of naming the missing
  optional dependency and the alternatives.
- **`materials` and `seed` were unreachable** from `create_model`, though both
  existed on `ModelSpec`.
- **OpenSCAD source detection required the `module` keyword**, so a plain
  `difference() { ... }` file was routed to the wrong backend.
- **`body_count` crashed without scipy**, taking the whole inspection with it.

### Added

- `platform_support` module: per-OS binary discovery (including app bundles on
  macOS and versioned installs on Windows), UTF-8 subprocess wrapper with a hard
  timeout, `CREATE_NO_WINDOW` on Windows, atomic file writes, and thread-safe
  timeouts.
- **`doctor`** — new CLI command and MCP tool. Reports OS, Python, CPU, every
  engine's availability with its resolved path, optional packages, workdir
  writability, and copy-pasteable install commands for the current OS. Reports
  whether an env var is set, never its value.
- `FORGE3D_OPENSCAD`, `FORGE3D_BLENDER` and `FORGE3D_THREE_URL` environment
  overrides.
- Viewer pages now explain a blocked three.js CDN instead of showing a blank
  canvas, and can be pointed at a local three.js mirror for air-gapped use.
- `Dockerfile` (non-root, healthcheck, `/work` volume) and `.dockerignore`.
- `.gitattributes` normalising line endings across platforms.
- CI matrix: Linux, macOS and Windows × Python 3.10 and 3.12, an engine job that
  installs OpenSCAD, and a wheel job that verifies the viewer assets ship inside
  the package.
- `docs/platforms.md` — the support matrix, including what does *not* work on
  each platform and why.
- `tests/test_platform.py` and `tests/test_regressions.py` (68 new tests).

### Changed

- Viewer docs corrected: the *model* is embedded and needs no server, but
  three.js still loads from a CDN by default. Air-gapped operation is supported
  and now documented as configuration rather than a default.
- `safe_timeout` documents its real limitation: Python cannot kill a thread, so
  a timed-out build is abandoned, not stopped. Containment needs a process
  boundary.

## [0.1.0] — 2026-11-01

First public release.

### Added

- **MCP server** (`forge3d-mcp`) with 13 tools over stdio or streamable HTTP:
  `list_backends`, `plan_model`, `create_model`, `get_job`, `list_jobs`,
  `inspect_model`, `validate_model`, `repair_model`, `convert_model`,
  `render_model`, `make_viewer`, `model_card`, `checklist`, `create_handoff`,
  `review_model`.
- **Five geometry engines** behind one interface: `procedural` (trimesh),
  `openscad`, `blender`, `cadquery`, `hosted` (text-to-3D adapter), with a
  data-driven `auto` routing table.
- **Profile-based validation** for `print`, `web`, `game`, `cad` and `any`.
  Every failing check carries a fix hint; severities separate blockers from
  judgement calls.
- **Repair**: merge, drop degenerate/duplicate faces, re-orient normals, fill
  holes, optional quadric decimation.
- **CPU preview renders** (matplotlib, no GPU) so agents can look at what they
  built.
- **Three.js viewer** in two forms: a single self-contained HTML page with the
  GLB embedded as a data URI, and a `<forge3d-viewer>` custom element for
  embedding in an existing app.
- **Model cards** — ~300-token summaries so geometry never enters a context
  window.
- **Multi-agent handoff** packets (JSON + Markdown) with named owners, open
  questions and checklists per delivery target.
- **Durable job records** for slow generations.
- **CLI** mirroring every capability without an agent.
- Examples: parametric chair (trimesh), pipe bracket (OpenSCAD), bevelled gear
  (bpy), machined plate with STEP export (CadQuery).
- Agent skill (`skill/SKILL.md`) plus references for backends, checklists,
  Three.js integration and prompt writing.

### Known limitations

- Local engines produce geometry, not painted PBR materials. Route to a hosted
  service for textured output.
- The hosted adapter speaks one documented contract; provider APIs differ and
  require a thin worker in front of them.
- No automatic rigging or weight painting.
- Preview renders are review-quality, not beauty shots.
