# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/).

## [0.6.1] — 2026-11-01

Documentation and code audit. Five stale claims in the docs and four real code
defects, all fixed.

### Fixed — code

- **`forge3d providers --check <unknown>` crashed** with a raw `KeyError`
  traceback. It now reports the failure and the valid names, like every other
  bad-input path in the CLI.
- **Tripo progress was wrong at exactly 1%.** The guard `progress / 100 if
  progress > 1 else progress` turned a reported 1% into 100%. Progress is now
  always treated as a 0-100 percentage, clamped.
- **`FORGE3D_HOSTED_RETRIES` was silently ignored.** The adapters hardcoded
  `attempts=3`, so the documented setting did nothing. `request_with_retries`
  now resolves the count from the env var when none is passed.
- Dead `PREAMBLE` constant removed from the procedural backend.

### Fixed — documentation

- `README.md` said "five interchangeable geometry engines" (and headed the table
  "The five engines") while listing six.
- `docs/architecture.md` said "MCP: 16 tools" against an actual 21.
- `.env.example` had no provider variables at all — `FORGE3D_PROVIDER` and the
  three key variables were undocumented.
- `docs/platforms.md` support matrix was missing the `image` backend and did not
  mention that the `hosted` row covers Meshy/Tripo/Rodin keys.

### Verified

- All 34 modules import cleanly; every internal markdown link resolves; every
  `FORGE3D_*` variable named in the docs exists in code (the one exception,
  `FORGE3D_LUMA_KEY`, is a deliberate example of writing a new adapter).

### Tests

345 passing (was 342). Adds the retry-count regression and the unknown-provider
report.

## [0.6.0] — 2026-11-01

Provider adapters: real integrations for Meshy, Tripo and Rodin behind one
interface, without breaking the generic worker.

### Added

- **`forge3d.providers`** — a job-shaped provider contract
  (`submit` / `get_status` / `download` / `cancel` / `check` / `estimate`) with
  five normalised statuses. Unknown status words map to `running`, not `failed`:
  an unrecognised string is not evidence of failure.
- **Adapters**: `meshy`, `tripo`, `rodin`, plus `generic` (the original worker
  contract, unchanged). Each declares `verified` — only `generic` is `True`.
- **`list_providers`** MCP tool (21 tools) and **`forge3d providers [--check]`** —
  what is configured, what is verified, and a live authenticated probe on demand.
- `FORGE3D_PROVIDER` selects a provider; `FORGE3D_MESHY_KEY` / `_TRIPO_KEY` /
  `_RODIN_KEY` hold credentials; each adapter's base URL is overridable for
  regional endpoints and tests.
- `HostedBackend.estimate()` — credits and ETA from the selected provider before
  anything is spent.
- `docs/providers.md` — the interface, how to add one, and what is missing.

### Fixed

- **A synchronous worker response crashed.** `GenericWorkerProvider.submit()`
  referenced `SUCCEEDED` without importing it, so any worker that answered with an
  `asset_url` immediately — instead of a `job_id` — raised `NameError`. Lint found
  it; a test now covers that branch.

### Notes

The commercial adapters are written from public API documentation and are **not
verified against live accounts**. Their mapping logic is tested against the
response shapes those docs describe, and `check()` settles the rest with a real
request. `list_providers` reports the distinction rather than hiding it.

### Tests

342 passing (was 310). New `tests/test_providers.py` (32) covers request mapping
per adapter, status normalisation, application-level error codes, registry
selection, and the hosted backend driven end-to-end by a fake provider.

## [0.5.1] — 2026-11-01

Fixes an external review's P0 finding: the local/cloud guarantee was documented
but not enforced through every public interface.

### Fixed

- **`spec.mode` was unreachable from every tool.** `_spec()` did not pass it,
  `plan_model()` and `create_model()` did not accept it, and the CLI had no
  `--mode` flag. The field existed, the router honoured it, and no caller could
  set it. All three interfaces now expose it.
- **An explicitly named backend bypassed the mode check.** `backends.run()` only
  consulted `resolve_auto()` when the backend was `"auto"`, so
  `backend="hosted", mode="local"` still made a billable call. Mode is now
  enforced at the single point every generation passes through, and the refusal
  explains itself.
- Server module docstring said "sixteen tools" while the surface had twenty.

### Tests

299 passing (was 291). New `tests/test_mode.py` covers the violation matrix, the
MCP tools, and the CLI flag.

## [0.5.0] — 2026-11-01

Parametric surfaces: 15 shape families defined by equations, so an agent can make
"anything" by naming a formula instead of writing vertex loops.

### Added

- **`forge3d.geometry.surfaces`** — a catalogue of equation-driven shapes, each
  with its formula, parameter documentation and a `closed` flag:

  **Solids** (watertight, printable): `supershape` (Gielis superformula),
  `spherical_harmonic`, `superellipsoid`, `torus_knot` (p,q), `lissajous`,
  `spring`, `seashell`, `involute_gear` (real meshing tooth profile),
  `twisted_prism`, `lathe` (solid of revolution from a profile), `bumpy_sphere`.

  **Open surfaces** (visual only, honestly marked): `mobius`, `helicoid`,
  `catenoid`. **Fractal**: `menger_sponge` (multi-body).

- **`create_shape`** MCP tool — build any family, export to glb/stl/obj/ply/off/
  gltf/dae, validate, preview, and optionally wrap in a viewer page.
- **`list_shapes`** MCP tool — returns the catalogue with equations and defaults,
  so an agent can choose a family rather than guess parameter names.
- `docs/surfaces.md` — every equation written out, with parameter tables and a
  resolution/face-count guide.

### Fixed

- **Caps produced non-watertight "solids".** Pole rings that collapse to a point
  left zero-area triangles after `process=True` deleted them, so eleven shapes
  claimed to be closed and leaked. A single ring-based primitive with explicit
  apex/flat caps and a degenerate-face filter replaced four bespoke builders;
  every closed shape is now watertight with consistent winding, asserted per shape
  by the test suite.
- **Tube caps were wound inconsistently**, leaving `spring` and `seashell`
  inside-out at the ends.

### Tests

291 passing (was 224). New `tests/test_surfaces.py` (67) iterates the whole
catalogue and asserts, per shape, that it builds, is finite, is deterministic,
and — when it claims to be closed — is watertight, consistently wound, and
positive-volume.

## [0.4.0] — 2026-11-01

Response to an external review of the repo (a ChatGPT transcript): procedural
geometry generators, a multi-model scene viewer, and a local/cloud safety rule.
The review's two biggest gaps — no planet generator, no scene editor — were also
exactly what its own Mars reference scene needed.

### Added

- **`forge3d.geometry.planets`** — deterministic procedural planets:
  - noise sampled in **3D on the unit sphere**, so the surface has no UV seam;
  - analytic **craters** with a bowl *and* a raised rim (noise alone reads as dimples);
  - optional **great-circle canyon** with ridged walls;
  - **spherical UVs** with baked **albedo / normal / metallic-roughness** maps
    generated from the same fields the geometry uses, so colour and silhouette agree.
  - everything seeded: same params, byte-identical output.
- **`create_planet`** MCP tool (18 tools total) and **`generate_scene`** for
  multi-body scenes.
- **`make_scene_viewer`** — composes several GLBs into one self-contained
  Three.js page with a starfield, a shared sun, orbit controls and snapshots.
  The single-model viewer could not express the four-planet reference scene.
- **`ModelSpec.mode`** (`auto` / `local` / `cloud`). `local` never routes to a
  paid provider and `cloud` never routes to a local engine. Silently upgrading a
  user into a billable API call is not a feature.
- `docs/geometry.md` — parameters, the Mars recipe, size budget, and what the
  generator deliberately does not do.

### Fixed

- **Canyon displacement scaled by radius instead of depth**, producing a
  displacement larger than the planet itself (relief range -1.82 where it should
  be -0.07). Caught by asserting the relief range stays inside the radius.
- **`roughnessTexture` was silently dropped on GLB export.** glTF packs
  roughness and metallic into one map (`G`/`B`); the standalone image was written
  but never referenced, so the material looked flat in the viewer.
- **The `purpose == "web"` routing branch bypassed `spec.mode`**, so a
  `mode="local"` request could still reach a hosted provider.

### Tests

224 passing (was 198). New `tests/test_planets.py` (26) covers determinism,
seam-free UVs, crater/canyon effects, PBR round-trip, scene composition, title
escaping, and the local/cloud routing guarantee.

## [0.3.0] — 2026-11-01

Secure execution, real image-to-3D, and production reliability. Also fixes the
two CI failures that the v0.2.1 release shipped with.

### Fixed (CI)

- **Ubuntu jobs failed:** the CPU rlimit fires as `SIGXCPU` (-24) *before* the
  parent's wall-clock timeout on a fast runner, so the failure fell through to a
  generic "exit -24" message and the test's assertion on "killed" failed. Return
  codes are now decoded to signal names, and both paths say what happened.
- **Windows jobs failed:** the parent decoded child output as UTF-8, but the
  child still *emitted* cp1252, so printing a Devanagari string raised
  UnicodeEncodeError. Children now get `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1`.

### Secure execution

- **Environment scrubbing.** Child processes no longer inherit the parent's
  environment. Anything matching `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|
  PRIVATE|SESSION|COOKIE` is dropped, so `FORGE3D_HOSTED_KEY` and cloud
  credentials are unreachable from agent-written code.
- **`FORGE3D_SANDBOX`** — `auto` (default) uses **bubblewrap** when present:
  read-only root, private `/tmp`, no network, own PID namespace, write access only
  to the work directory. `unshare` is opt-in because it needs capabilities most
  containers withhold. The job metadata records the isolation that actually ran
  (`bwrap` / `unshare-net` / `rlimit`), and `doctor` self-tests the wrapper instead
  of assuming it works.
- **More rlimits:** `RLIMIT_FSIZE` (512 MiB), `RLIMIT_NOFILE` (256),
  `RLIMIT_CORE` (0), on top of CPU and optional address space.
- **A lesson worth recording:** the first scrub used a fixed allowlist and broke
  `pip install --user` installs by dropping `PYTHONUSERBASE` — the child could no
  longer import forge3d. The policy is now pattern-based with an explicit deny
  list, and a test asserts secrets are gone *and* the interpreter still works.

### Real image-to-3D

- **New `image` backend** — geometry derived from pixels, deterministic, no GPU:
  - `relief`: luminance → height on a solid base (plaques, lithophanes, height maps)
  - `silhouette`: alpha/luminance → extruded profile with holes preserved
  - `auto`: transparent background → silhouette, else relief
- Output is watertight with consistent winding; the silhouette test asserts the
  exact analytic volume of a ring ($$1-(14/40)^2$$ of the disc) to within 4%.
- **Budget-aware resolution:** a relief costs $$4(g-1)^2+8(g-1)$$ triangles, and
  the grid is shrunk to fit `poly_budget` before generating rather than decimated
  afterwards. A 512 grid is ~1.05M triangles and was a genuine production hazard.
- `networkx` added as a dependency: `trimesh.repair.fix_winding` needs it, so
  `repair_model` would have raised ModuleNotFoundError on a broken mesh.

### Production reliability

- **`GET /health`** — unauthenticated, reports version, uptime, exec mode, the
  sandbox in force, and job counts. Never a token or key.
- **Concurrency limiting** — `FORGE3D_MAX_CONCURRENT` (default 2) with a bounded
  queue (`FORGE3D_QUEUE_WAIT`, default 30 s), then an explicit "server busy".
  Slots are released in `finally`; a failed build cannot leave the server stuck.
- **Rate limiting** — `FORGE3D_RATE_LIMIT` per token/IP, `429` with `Retry-After`.
- **Retries** — hosted calls back off exponentially with jitter on 5xx and
  connection errors, never on 4xx.
- **Structured logs** — `FORGE3D_LOG_JSON=1` emits one JSON line per tool call
  (via an MCP middleware, so stdio clients are covered) and per HTTP request.
- New docs: `docs/image-to-3d.md`, `docs/reliability.md`.

### Tests

198 passing (was 162). New suites: `test_image_backend.py` (20) and
`test_reliability.py` (16).

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
