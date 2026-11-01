/**
 * <forge3d-viewer> — drop-in Three.js viewer for your own app.
 *
 *   <script type="module" src="https://your-host/forge3d-viewer.js"></script>
 *   <forge3d-viewer src="/models/chair.glb" height="480" autorotate></forge3d-viewer>
 *
 * Attributes: src, height, autorotate, grid, wireframe, background, exposure
 * Properties: model (THREE.Object3D), scene, camera, renderer, controls
 * Events: forge3d:loaded (detail: {triangles}), forge3d:error (detail: {error})
 *
 * Requires three >= 0.160 to be resolvable from the page's import map, e.g.
 *   <script type="importmap">
 *   {"imports":{"three":"https://unpkg.com/three@0.169.0/build/three.module.js",
 *               "three/addons/":"https://unpkg.com/three@0.169.0/examples/jsm/"}}
 *   </script>
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const template = document.createElement('template');
template.innerHTML = `
  <style>
    :host { display: block; position: relative; overflow: hidden;
      border-radius: var(--forge3d-radius, 14px); background: var(--forge3d-bg, #0b0d10); }
    canvas { display: block; width: 100%; height: 100%; }
    .badge { position: absolute; bottom: 10px; right: 12px; font: 11px/1.4 ui-sans-serif, system-ui, sans-serif;
      color: rgba(255,255,255,.42); pointer-events: none; }
  </style>
  <canvas></canvas>
  <div class="badge">forge3d</div>
`;

class Forge3DViewer extends HTMLElement {
  static get observedAttributes() {
    return ['src', 'autorotate', 'grid', 'wireframe', 'background', 'exposure'];
  }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).appendChild(template.content.cloneNode(true));
    this.canvas = this.shadowRoot.querySelector('canvas');
    this.model = null;
    this._ready = false;
    this._observer = new ResizeObserver(() => this.resize());
  }

  connectedCallback() {
    this.height = this.getAttribute('height') || '420';
    this.style.height = `${this.height}px`;
    this._init();
    this._observer.observe(this);
    if (this.getAttribute('src')) this.load(this.getAttribute('src'));
  }

  disconnectedCallback() {
    this._observer.disconnect();
    cancelAnimationFrame(this._frame);
    this.renderer?.dispose();
  }

  attributeChangedCallback(name, oldVal, newVal) {
    if (oldVal === newVal) return;
    if (name === 'src' && newVal && this._ready) this.load(newVal);
    if (name === 'autorotate' && this.controls) this.controls.autoRotate = this.hasAttribute('autorotate');
    if (name === 'grid' && this.grid) this.grid.visible = this.hasAttribute('grid');
    if (name === 'background' && this.scene) this.scene.background = new THREE.Color(newVal);
    if (name === 'exposure' && this.renderer) this.renderer.toneMappingExposure = parseFloat(newVal) || 1;
    if (name === 'wireframe' && this.model) {
      const on = this.hasAttribute('wireframe');
      this.model.traverse((o) => {
        if (o.isMesh && o.material) {
          (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => (m.wireframe = on));
        }
      });
    }
  }

  _init() {
    if (this._ready) return;
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = parseFloat(this.getAttribute('exposure') || '1.05');

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(this.getAttribute('background') || '#0b0d10');
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

    this.camera = new THREE.PerspectiveCamera(38, 1, 0.01, 10000);
    this.camera.position.set(2.2, 1.6, 2.6);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.autoRotate = this.hasAttribute('autorotate');
    this.controls.autoRotateSpeed = 1.1;

    const key = new THREE.DirectionalLight(0xffffff, 2.3);
    key.position.set(4, 7, 5);
    key.castShadow = true;
    this.scene.add(key, new THREE.HemisphereLight(0xcfe0ff, 0x14171c, 0.5));

    this.grid = new THREE.GridHelper(10, 20, 0x2a3444, 0x1a2029);
    this.grid.visible = this.hasAttribute('grid');
    this.scene.add(this.grid);

    this.root = new THREE.Group();
    this.scene.add(this.root);

    this._ready = true;
    this.resize();
    this._tick();
  }

  load(url) {
    const loader = new GLTFLoader();
    loader.load(
      url,
      (gltf) => {
        if (this.model) this.root.remove(this.model);
        this.model = gltf.scene;
        this.model.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
        this.root.add(this.model);
        this._fit();
        let tris = 0;
        this.model.traverse((o) => {
          if (o.isMesh && o.geometry) {
            const i = o.geometry.index;
            tris += i ? i.count / 3 : (o.geometry.attributes.position?.count || 0) / 3;
          }
        });
        this.dispatchEvent(new CustomEvent('forge3d:loaded', { detail: { triangles: Math.round(tris) } }));
      },
      undefined,
      (err) => this.dispatchEvent(new CustomEvent('forge3d:error', { detail: { error: err?.message || 'load failed' } }))
    );
  }

  _fit() {
    const box = new THREE.Box3().setFromObject(this.model);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const dist = (maxDim / (2 * Math.tan((this.camera.fov * Math.PI) / 360))) * 1.9;
    this.controls.target.copy(center);
    this.camera.position.set(center.x + dist * 0.72, center.y + dist * 0.52, center.z + dist * 0.72);
    this.camera.near = maxDim / 500;
    this.camera.far = maxDim * 500;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.grid.scale.setScalar(maxDim / 5);
  }

  resize() {
    if (!this._ready) return;
    const w = this.clientWidth || 640;
    const h = this.clientHeight || parseInt(this.height, 10) || 420;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  _tick() {
    this._frame = requestAnimationFrame(() => this._tick());
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}

customElements.define('forge3d-viewer', Forge3DViewer);
export { Forge3DViewer };
