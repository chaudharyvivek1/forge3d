"""Safe outbound fetching: SSRF guards, size caps, and content validation.

The image backend used to do `requests.get(url)` and `resp.content`. That is a
server-side request forgery primitive: a caller could ask forge3d to fetch
`http://169.254.169.254/latest/meta-data/` (cloud metadata), `http://localhost:9200`
(an internal search index), or a 4 GB file, and the server would happily do it and
hold the whole thing in memory.

Two generations of hardening live here:

**v0.6.2** blocked private ranges and validated redirects. That was not enough.
Two gaps remained, both fixed in v0.6.3:

1. **DNS rebinding (time-of-check to time-of-use).** Resolving a hostname to check
   it, then handing the *hostname* to an HTTP client, means the client resolves it
   again — and an attacker-controlled name can answer public the first time and
   private the second. The fix is to **connect to the validated IP** while keeping
   the hostname for TLS verification (`server_hostname` / `assert_hostname`), so
   the address that was checked is the address that is reached.
2. **Blocklist vs allowlist.** `_is_blocked_ip` enumerated bad ranges, and
   `100.64.0.0/10` (carrier-grade NAT) is neither `is_private` nor `is_reserved`
   — it slipped through. The rule is now inverted: **an address is only allowed if
   `is_global` is true**, which is the definition of "routable on the public
   internet" and cannot fall out of date.

Proxies are the honest caveat: an HTTP proxy performs its own DNS resolution, so
IP pinning cannot apply through one. When a proxy is configured the destination is
still validated, the result is marked `proxied=True`, and
`FORGE3D_ALLOW_PROXY_FETCH=false` refuses such fetches outright.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3

DEFAULT_MAX_BYTES = 25 * 1024 * 1024        # 25 MiB is generous for a reference image
DEFAULT_MAX_PIXELS = 40_000_000             # ~6300x6300, well past any texture
ALLOWED_SCHEMES = ("http", "https")
ALLOWED_IMAGE_TYPES = (
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp",
    "image/tiff", "image/gif",
)
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


class UnsafeURLError(ValueError):
    """The URL was refused before or during fetching."""


@dataclass
class FetchResult:
    url: str
    content: bytes
    content_type: str
    final_url: str
    size: int
    ip: str | None = None
    proxied: bool = False

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.content)
        return p


# ---------------------------------------------------------------------------
# address policy
# ---------------------------------------------------------------------------
def _blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address must not be fetched, or None if it is publicly routable.

    The rule is positive, not a list of known-bad ranges: only `is_global`
    addresses pass. That covers loopback, private, link-local, reserved,
    multicast, unspecified **and** special cases like carrier-grade NAT
    (100.64.0.0/10), which is neither private nor reserved in Python's model.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _blocked_reason(ip.ipv4_mapped)
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (cloud metadata lives here)"
    if ip.is_private:
        return "private address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        # 100.64.0.0/10, 192.0.0.0/24, 198.18.0.0/15 and friends land here.
        return "non-public (not globally routable) address"
    return None


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Backwards-compatible alias."""
    return _blocked_reason(ip)


def resolve_and_check(url: str, allow_private: bool = False) -> list[str]:
    """Validate the scheme and every address the host resolves to."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"scheme {parsed.scheme!r} is not allowed; only http and https"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if allow_private:
        return []

    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve {host!r}: {exc}") from exc

    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise UnsafeURLError(f"{host!r} resolved to no addresses")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        reason = _blocked_reason(ip)
        if reason:
            raise UnsafeURLError(f"{host!r} resolves to {raw}, a {reason}")
    return addresses


def check_url(url: str, allow_private: bool = False) -> None:
    """Refuse a URL whose scheme or resolved addresses are not safe to fetch."""
    resolve_and_check(url, allow_private=allow_private)


def _proxy_configured() -> bool:
    if os.environ.get("FORGE3D_ALLOW_PROXY_FETCH", "true").lower() in ("0", "false", "no"):
        for var in _PROXY_VARS:
            if os.environ.get(var):
                raise UnsafeURLError(
                    "a proxy is configured and FORGE3D_ALLOW_PROXY_FETCH is false; "
                    "a proxy resolves destinations itself, so address pinning cannot apply"
                )
    return any(os.environ.get(var) for var in _PROXY_VARS)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
class _PinnedStream:
    """Minimal response wrapper over a urllib3 response."""

    def __init__(self, resp, url: str, ip: str):
        self._resp = resp
        self.url = url
        self.ip = ip
        self.status_code = int(resp.status)
        self.headers = {k.lower(): v for k, v in resp.headers.items()}
        self.is_redirect = 300 <= self.status_code < 400

    def iter_content(self, chunk_size: int = 1 << 16):
        while True:
            chunk = self._resp.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._resp.release_conn()


def _open_pinned(url: str, ip: str, timeout: int, headers: dict) -> _PinnedStream:
    """Connect to `ip`, while presenting the URL's hostname to the server.

    This is the whole point: the address that was validated is the address that
    is dialled. For HTTPS, `server_hostname` keeps SNI and certificate
    verification bound to the real hostname, so pinning does not weaken TLS.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    https = parsed.scheme.lower() == "https"
    port = parsed.port or (443 if https else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    request_headers = dict(headers)
    request_headers["Host"] = host
    timeout_obj = urllib3.util.Timeout(connect=timeout, read=timeout)

    if https:
        pool = urllib3.HTTPSConnectionPool(
            ip, port=port, timeout=timeout_obj,
            server_hostname=host, assert_hostname=host,
            cert_reqs="CERT_REQUIRED",
        )
    else:
        pool = urllib3.HTTPConnectionPool(ip, port=port, timeout=timeout_obj)

    resp = pool.urlopen("GET", path, headers=request_headers,
                        redirect=False, preload_content=False, retries=False)
    return _PinnedStream(resp, url, ip)


def _open_via_proxy(url: str, timeout: int, headers: dict):
    """Fallback when a proxy is in play: the proxy does its own resolution."""
    resp = requests.get(url, stream=True, timeout=timeout, allow_redirects=False,
                        headers=headers)
    return resp


def open_stream(url: str, timeout: int = 30, allow_private: bool = False,
                headers: dict | None = None):
    """Open a validated, address-pinned response for `url`.

    Returns an object with `.status_code`, `.headers`, `.iter_content()` and
    `.close()`. Redirects are **not** followed here; the caller re-runs this for
    each hop, so every hop is validated and pinned.
    """
    addresses = resolve_and_check(url, allow_private=allow_private)
    hdrs = {"Accept": "*/*", **(headers or {})}

    if _proxy_configured():
        return _open_via_proxy(url, timeout, hdrs), True

    if allow_private:
        return _open_via_proxy(url, timeout, hdrs), False

    return _open_pinned(url, addresses[0], timeout, hdrs), False


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def fetch_bytes(
    url: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = 30,
    max_redirects: int = 3,
    allow_private: bool = False,
) -> FetchResult:
    """Fetch a URL with SSRF guards, a byte ceiling, and redirect revalidation."""
    max_bytes = max(1, int(max_bytes))
    current = url

    for hop in range(max_redirects + 1):
        # Validate here as well as inside open_stream: the guard must hold even if
        # the transport is swapped, and this is the line a test can pin down.
        resolve_and_check(current, allow_private=allow_private)
        stream, proxied = open_stream(current, timeout=timeout,
                                      allow_private=allow_private)
        try:
            status = stream.status_code
            headers = {k.lower(): v for k, v in stream.headers.items()}

            if 300 <= status < 400:
                location = headers.get("location")
                if not location:
                    raise UnsafeURLError(f"{current} redirected without a Location")
                if hop >= max_redirects:
                    raise UnsafeURLError(f"too many redirects (>{max_redirects})")
                current = requests.compat.urljoin(current, location)
                continue

            if status >= 400:
                raise UnsafeURLError(f"{current} returned HTTP {status}")

            declared = (headers.get("content-type") or "").split(";")[0].strip().lower()
            if declared and declared not in ALLOWED_IMAGE_TYPES:
                raise UnsafeURLError(
                    f"content-type {declared!r} is not an accepted image type"
                )

            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise UnsafeURLError(
                    f"declared size {int(length)} bytes exceeds the {max_bytes} byte limit"
                )

            chunks, total = [], 0
            for chunk in stream.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeURLError(
                        f"response exceeded the {max_bytes} byte limit while streaming"
                    )
                chunks.append(chunk)

            return FetchResult(
                url=url, content=b"".join(chunks), content_type=declared,
                final_url=current, size=total,
                ip=getattr(stream, "ip", None), proxied=proxied,
            )
        finally:
            stream.close()

    raise UnsafeURLError("redirect loop")


def fetch_to_file(url: str, dest: str | Path, max_bytes: int = 512 * 1024 * 1024,
                  timeout: int = 300, max_redirects: int = 3,
                  allow_private: bool = False) -> Path:
    """Stream a URL straight to disk with a byte ceiling.

    Used for provider asset downloads, where holding a 500 MB GLB in memory would
    be its own denial of service. Same guard as `fetch_bytes` — the destination is
    validated and the connection is pinned, because a provider (or a compromised
    generic worker) can return any URL it likes.
    """
    max_bytes = max(1, int(max_bytes))
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    current = url

    for hop in range(max_redirects + 1):
        resolve_and_check(current, allow_private=allow_private)
        stream, _proxied = open_stream(current, timeout=timeout,
                                       allow_private=allow_private)
        try:
            status = stream.status_code
            headers = {k.lower(): v for k, v in stream.headers.items()}

            if 300 <= status < 400:
                location = headers.get("location")
                if not location:
                    raise UnsafeURLError(f"{current} redirected without a Location")
                if hop >= max_redirects:
                    raise UnsafeURLError(f"too many redirects (>{max_redirects})")
                current = requests.compat.urljoin(current, location)
                continue

            if status >= 400:
                raise UnsafeURLError(f"{current} returned HTTP {status}")

            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise UnsafeURLError(
                    f"declared size {int(length)} bytes exceeds the {max_bytes} byte limit"
                )

            written = 0
            with open(target, "wb") as fh:
                for chunk in stream.iter_content(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > max_bytes:
                        fh.close()
                        target.unlink(missing_ok=True)
                        raise UnsafeURLError(
                            f"download exceeded the {max_bytes} byte limit"
                        )
                    fh.write(chunk)
            return target
        finally:
            stream.close()

    raise UnsafeURLError("redirect loop")


def fetch_image(url: str, dest: str | Path, max_bytes: int = DEFAULT_MAX_BYTES,
                max_pixels: int = DEFAULT_MAX_PIXELS, allow_private: bool = False) -> Path:
    """Fetch a remote image, verify it really is one, and save it.

    The decode step matters: a `content-type: image/png` header proves nothing,
    and a decompression bomb is a small file that explodes in memory when decoded.
    """
    result = fetch_bytes(url, max_bytes=max_bytes, allow_private=allow_private)
    validate_image_bytes(result.content, max_pixels=max_pixels)

    suffix = Path(urlparse(result.final_url).path).suffix or ".png"
    target = Path(dest)
    if target.suffix.lower() != suffix.lower():
        target = target.with_suffix(suffix)
    return result.save(target)


def validate_image_bytes(content: bytes, max_pixels: int = DEFAULT_MAX_PIXELS) -> tuple[int, int]:
    """Refuse bytes that are not a decodable image within the pixel budget."""
    from PIL import Image

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        import io

        with Image.open(io.BytesIO(content)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(content)) as probe:
            width, height = probe.size
            if width * height > max_pixels:
                raise UnsafeURLError(
                    f"image is {width}x{height} = {width * height} pixels, "
                    f"above the {max_pixels} pixel budget"
                )
            return width, height
    except UnsafeURLError:
        raise
    except Image.DecompressionBombError as exc:
        raise UnsafeURLError(
            f"image exceeds the {max_pixels} pixel budget (decompression bomb guard)"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not an image"
        raise UnsafeURLError(f"downloaded bytes are not a decodable image: {exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous
