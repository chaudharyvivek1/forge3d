# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/).

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
