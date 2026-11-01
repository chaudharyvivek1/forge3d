# Three.js — viewer, embedding, and app integration

## Two artifacts

### 1. Single-file page (`make_viewer`)

The GLB is embedded as a base64 data URI. One file, works offline, no server, no
CORS. Best for: sending a model to a designer or client, attaching to a ticket,
opening from a filesystem path.

```bash
forge3d viewer output/chair.glb --out chair.html
```

Cost: base64 inflates the payload ~33%. Above roughly 15 MB of GLB, host the file
and use the custom element instead.

Features built in: orbit/zoom, auto-rotate toggle, wireframe, grid, PNG
snapshot, reset view, drag-and-drop a local `.glb` to inspect it, live triangle
count and dimensions in the HUD. Keyboard: `W` wireframe, `G` grid, `S`
snapshot, `R` reset.

### 2. Custom element (`forge3d-viewer.js`)

For your own app. Takes a URL, so the mesh stays out of the HTML.

```html
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.169.0/build/three.module.js",
            "three/addons/":"https://unpkg.com/three@0.169.0/examples/jsm/"}}
</script>
<script type="module" src="/static/forge3d-viewer.js"></script>

<forge3d-viewer
  src="/models/chair.glb"
  height="480"
  autorotate
  grid
  background="#0b0d10"
  exposure="1.05"></forge3d-viewer>
```

Properties: `.model` (the loaded `THREE.Object3D`), `.scene`, `.camera`,
`.renderer`, `.controls`. Events: `forge3d:loaded` with `detail.triangles`,
`forge3d:error` with `detail.error`. Theming through CSS variables
`--forge3d-bg` and `--forge3d-radius`.

Copy it into your app with:

```python
from forge3d.viewer import copy_web_component
copy_web_component("static/")
```

## Performance rules that actually matter

| Target | Triangle budget | Notes |
|---|---|---|
| mobile web hero asset | ≤ 100k | one model on screen |
| mobile web grid of models | ≤ 15k each | many draw calls dominate |
| desktop web | ≤ 250k | still instantiate lazily |
| VR | ≤ 100k | frame budget is 11 ms |

- **Draw calls beat triangle count.** Ten meshes with ten materials is slower
  than one mesh with one material at twice the triangles. Merge in
  `repair_model` or at export time.
- **Textures dominate download size**, not geometry. A 2048² PNG is bigger than
  200k triangles. Use KTX2/Basis for anything you ship at scale.
- **Enable `renderer.shadowMap` only if you need it.** It roughly doubles the
  vertex work.
- **Dispose on unload.** Both artifacts call `dispose()` on geometry, materials
  and textures when a model is replaced. If you build your own viewer, do the
  same — leaks are the number one Three.js bug in agent-generated code.

## Lighting setup used here (and why)

```
PMREMGenerator + RoomEnvironment  → soft, physically-plausible ambient
DirectionalLight (castShadow)     → one clear key light
HemisphereLight                   → fills the shadow side with sky/ground colour
ACESFilmicToneMapping             → stops highlights clipping to flat white
```

Agents frequently generate `AmbientLight(0xffffff, 1)` plus nothing else, which
is why AI-written Three.js scenes look flat. Environment lighting is the single
biggest visual upgrade for one line of code.

## Loading other formats

`GLTFLoader` handles `.glb` and `.gltf`. For STL/OBJ/PLY you need
`STLLoader`, `OBJLoader`, `PLYLoader` — or convert first, which is simpler:

```bash
forge3d convert model.stl --fmt glb
```

Always convert. One loader, one code path, textures preserved.

## Embedding in a non-web app

- **Electron / Tauri**: the single-file HTML works as-is.
- **React**: wrap the custom element; `ref.current.model` gives you the object.
  Remember custom elements need their attributes as strings.
- **React Native / mobile**: use `expo-gl` + `expo-three` and load the GLB with
  `GLTFLoader` pointed at a bundled asset — the viewer code is the reference,
  not the runtime.
- **Blender / DCC round-trip**: export GLB, not FBX, unless the target needs
  FBX. GLB keeps PBR materials and is deterministic.
