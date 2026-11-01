# Providers

Text-to-3D and image-to-3D services, behind one interface. `HostedBackend` used
to speak only forge3d's own worker contract, which meant every user had to host a
shim in front of Meshy or Tripo. Now there are real adapters — and the generic
worker still works exactly as before.

```bash
forge3d providers              # what is configured here
forge3d providers --check      # live authenticated request
forge3d providers --check tripo
```

```python
list_providers()               # MCP tool, same information
create_model(backend="hosted", mode="cloud", prompt="a ceramic teapot")
```

## The honest part

Each adapter reports `verified`: **has the maintainer run it against a live
account?** Only the generic worker has.

| provider | verified | key | confidence |
|---|---|---|---|
| `generic` | ✅ | `FORGE3D_HOSTED_URL` | this *is* the contract; covered by tests |
| `meshy` | ❌ | `FORGE3D_MESHY_KEY` | written from public docs |
| `tripo` | ❌ | `FORGE3D_TRIPO_KEY` | written from public docs |
| `rodin` | ❌ | `FORGE3D_RODIN_KEY` | least standard API of the three |

The adapters are **not** claimed to work. They were written from public API
documentation, and their request/response mapping is tested against the response
shapes those docs describe — but no key has been used here. `check()` is how you
settle it in one command:

```
$ forge3d providers --check
meshy   ok: false   FORGE3D_MESHY_KEY is not set
```

That is deliberate. A silent "not integrated" wastes an afternoon; a loud
"unverified" costs you thirty seconds.

## The interface

```python
class GenerationProvider:
    name: str
    verified: bool
    key_env: str | None

    def available(self) -> bool            # are credentials present?
    def capabilities(self) -> dict          # text/image, textures, cancellable
    def estimate(self, request) -> dict     # credits, eta — before spending
    def submit(self, request) -> ProviderJob
    def get_status(self, job_id) -> ProviderJob
    def cancel(self, job_id) -> bool        # best effort
    def download(self, job, out_dir, name) -> Path
    def check(self) -> dict                 # live auth probe
```

Every provider is asynchronous, so everything is job-shaped. Statuses are
normalised onto five values:

```
queued · running · succeeded · failed · cancelled
```

Anything a provider calls something else (`PENDING`, `IN_PROGRESS`, `Done`,
`success`, `finished`…) is mapped in `normalise_status`. An **unknown** status word
becomes `running`, not `failed` — an unrecognised string is not evidence of
failure, and treating it as one aborts jobs that are fine.

## Adding one

```python
# src/forge3d/providers/luma.py
from .base import GenerationProvider, GenerationRequest, ProviderJob, normalise_status, request_with_retries

class LumaProvider(GenerationProvider):
    name = "luma"
    verified = False                 # be honest until you have run it
    key_env = "FORGE3D_LUMA_KEY"
    docs_url = "https://..."

    def submit(self, request):
        resp = request_with_retries("POST", f"{self.base_url()}/v1/gen",
                                    json={"prompt": request.prompt},
                                    headers=self._headers(), timeout=60)
        self._raise_for_status(resp, "submit")
        return ProviderJob(provider=self.name, job_id=self._json(resp)["id"])

    def get_status(self, job_id):
        resp = request_with_retries("GET", f"{self.base_url()}/v1/gen/{job_id}",
                                    headers=self._headers(), timeout=60)
        data = self._json(resp)
        return ProviderJob(provider=self.name, job_id=job_id,
                           status=normalise_status(data["state"]),
                           asset_url=data.get("glb"))
```

Then add it to `_CLASSES` in `registry.py` and write the mapping tests. Two
conventions worth keeping: read the base URL from an env var (so a regional
endpoint or a mock can be substituted), and treat an application-level error code
as a failure even when HTTP says 200 — Tripo does exactly this and it is the
easiest thing to get wrong.

## Selection

```
FORGE3D_PROVIDER=meshy        explicit, wins
FORGE3D_PROVIDER unset        first available commercial provider, in a fixed order
nothing configured            an error that names every env var you could set
```

`mode` still applies on top: `mode="local"` refuses the hosted backend even if you
name it explicitly, so a provider can never be reached by accident.

## Retries and money

`request_with_retries` backs off exponentially with jitter and retries **5xx and
connection errors only**. A 4xx is the caller's fault; retrying it burns the
provider's quota and your credits.

`estimate()` exists so a caller can budget before spending, and `mode="local"`
exists so a caller can refuse to spend at all.

## What is still missing

- **No webhooks.** Everything polls. Fine at this scale, wasteful at a thousand
  concurrent jobs — that is a queue's job, not an adapter's.
- **No cost tracking.** `estimate()` returns provider-specific hints, not a ledger.
  Per-user spend belongs in the platform layer.
- **No image upload.** Image-to-3D passes a URL; providers that need a multipart
  upload would need an extra step.
- **No cancellation on Rodin**, which exposes no cancel endpoint.
