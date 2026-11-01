"""A reference text-to-3D worker that implements forge3d's hosted contract.

forge3d deliberately does not ship a "universal" client for Meshy / Tripo /
Rodin / Hunyuan3D: their request shapes, auth schemes, polling semantics and
webhook behaviour differ, and a client that pretended otherwise would be wrong in
ways you would only discover in production. Instead it speaks one small contract
and gives you this worker to adapt.

Run it (stdlib only, no dependencies):

    python examples/hosted_worker.py --port 8100

Point forge3d at it:

    export FORGE3D_HOSTED_URL=http://127.0.0.1:8100
    forge3d create --name teapot --purpose web --backend hosted \
        --prompt "matte ceramic teapot, studio product render"

The worker below generates a placeholder mesh so the whole path is testable
end-to-end. Replace `generate_mesh()` with a real provider call — that is the
only function you need to touch.

Contract implemented:

    POST /generate
      { "prompt": str, "images": [url], "format": "glb", "seed": int|null,
        "art_style": str|null, "provider": str }
      -> 202 { "job_id": "..." }        async, poll /jobs/{id}
      -> 200 { "asset_url": "..." }     sync, done immediately

    GET /jobs/{job_id}
      -> { "status": "queued|running|succeeded|failed",
           "asset_url": "https://...", "error": str|null }

    GET /assets/{name}                  serves the generated file

Security note: this is a development harness. It has no authentication and
serves files from a temp directory. Do not expose it to the internet.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ASSET_DIR = Path(tempfile.mkdtemp(prefix="forge3d-hosted-"))
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# the only part you replace
# ---------------------------------------------------------------------------
def generate_mesh(prompt: str, out_path: Path, seed: int | None = None) -> None:
    """Produce a mesh for `prompt`.

    Replace this with a real provider call, for example:

        resp = requests.post(
            "https://api.provider.example/v2/text-to-3d",
            headers={"Authorization": f"Bearer {os.environ['PROVIDER_KEY']}"},
            json={"mode": "preview", "prompt": prompt, "art_style": "realistic"},
        )
        task_id = resp.json()["result"]
        # ... poll their task endpoint until status == "SUCCEEDED" ...
        glb = requests.get(result["model_urls"]["glb"]).content
        out_path.write_bytes(glb)

    The placeholder below is a deterministic placeholder so you can exercise the
    full pipeline — including forge3d's validation, which will correctly complain
    that a text-to-3D mesh is not watertight or to scale.
    """
    import trimesh

    mesh = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
    # A stand-in for "whatever the provider gave us": no UVs, no real scale.
    out_path.write_bytes(mesh.export(file_type="glb"))


# ---------------------------------------------------------------------------
# transport — you normally leave this alone
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        print(f"[hosted-worker] {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - stdlib naming
        if self.path != "/generate":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})

        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            return self._json(400, {"error": "prompt is required"})

        job_id = uuid.uuid4().hex[:12]
        with LOCK:
            JOBS[job_id] = {"status": "queued", "asset_url": None, "error": None,
                            "prompt": prompt}

        def work():
            with LOCK:
                JOBS[job_id]["status"] = "running"
            try:
                time.sleep(1.0)  # stand-in for provider latency
                out = ASSET_DIR / f"{job_id}.glb"
                generate_mesh(prompt, out, payload.get("seed"))
                with LOCK:
                    JOBS[job_id].update(
                        status="succeeded",
                        asset_url=f"http://{self.headers.get('host', '127.0.0.1')}"
                                  f"/assets/{out.name}",
                    )
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                with LOCK:
                    JOBS[job_id].update(status="failed", error=str(exc))

        threading.Thread(target=work, daemon=True).start()
        return self._json(202, {"job_id": job_id})

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.startswith("/jobs/"):
            job_id = self.path.split("/jobs/", 1)[1]
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self._json(404, {"error": "no such job"})
            return self._json(200, {
                "status": job["status"],
                "asset_url": job["asset_url"],
                "error": job["error"],
            })

        if self.path.startswith("/assets/"):
            name = Path(self.path.split("/assets/", 1)[1]).name  # no traversal
            path = ASSET_DIR / name
            if not path.is_file():
                return self._json(404, {"error": "no such asset"})
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "model/gltf-binary")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None

        if self.path == "/health":
            return self._json(200, {"ok": True, "jobs": len(JOBS)})

        return self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="forge3d reference hosted worker")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[hosted-worker] listening on http://{args.host}:{args.port}")
    print(f"[hosted-worker] assets in {ASSET_DIR}")
    print("[hosted-worker] set FORGE3D_HOSTED_URL to this address")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hosted-worker] stopped")


if __name__ == "__main__":
    main()
