"""Safe outbound fetching: SSRF guards, size caps, and content validation.

The image backend used to do `requests.get(url)` and `resp.content`. That is a
server-side request forgery primitive: a caller could ask forge3d to fetch
`http://169.254.169.254/latest/meta-data/` (cloud metadata), `http://localhost:9200`
(an internal search index), or a 4 GB file, and the server would happily do it and
hold the whole thing in memory.

Everything here is deliberately strict and deny-by-default:

* **scheme** — http/https only. No `file://`, no `gopher://`, no `ftp://`.
* **address** — every resolved IP is checked. Private, loopback, link-local,
  reserved, multicast and unspecified ranges are refused, **after every redirect
  hop** (a public host that 302s to `127.0.0.1` is the classic bypass).
* **size** — streamed with a hard byte ceiling, so a slow-loris or a giant file
  cannot exhaust memory or disk.
* **content** — the declared type must look like an image, and the bytes must
  actually decode as one, within a pixel budget.

This is not a complete egress firewall: a host you allow can still serve anything
it likes. For untrusted callers, combine it with a network policy.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

DEFAULT_MAX_BYTES = 25 * 1024 * 1024        # 25 MiB is generous for a reference image
DEFAULT_MAX_PIXELS = 40_000_000             # ~6300x6300, well past any texture
ALLOWED_SCHEMES = ("http", "https")
ALLOWED_IMAGE_TYPES = (
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp",
    "image/tiff", "image/gif",
)


class UnsafeURLError(ValueError):
    """The URL was refused before or during fetching."""


@dataclass
class FetchResult:
    url: str
    content: bytes
    content_type: str
    final_url: str
    size: int

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.content)
        return p


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address must not be fetched, or None if it is fine."""
    # Most specific reason first: `is_private` also covers loopback and
    # link-local, so checking it first would mislabel 127.0.0.1 and 169.254.x.
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (cloud metadata lives here)"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    # IPv6 that wraps an IPv4 address must be judged by the v4 rules.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(ip.ipv4_mapped)
    return None


def check_url(url: str, allow_private: bool = False) -> None:
    """Refuse a URL whose scheme or resolved addresses are not safe to fetch."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"scheme {parsed.scheme!r} is not allowed; only http and https"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if allow_private:
        return

    # A literal IP is checked directly; a hostname is resolved and every answer
    # is checked, because DNS can return a public and a private address.
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, parsed.port or 0)}
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve {host!r}: {exc}") from exc

    if not addresses:
        raise UnsafeURLError(f"{host!r} resolved to no addresses")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        reason = _is_blocked_ip(ip)
        if reason:
            raise UnsafeURLError(f"{host!r} resolves to {raw}, a {reason}")


def fetch_bytes(
    url: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = 30,
    max_redirects: int = 3,
    allow_private: bool = False,
    session: requests.Session | None = None,
) -> FetchResult:
    """Fetch a URL with SSRF guards, a byte ceiling, and redirect revalidation."""
    max_bytes = max(1, int(max_bytes))
    current = url
    http = session or requests

    for hop in range(max_redirects + 1):
        check_url(current, allow_private=allow_private)

        try:
            resp = http.get(current, stream=True, timeout=timeout,
                            allow_redirects=False,
                            headers={"Accept": "image/*"})
        except requests.RequestException as exc:
            raise UnsafeURLError(f"could not fetch {current}: {exc}") from exc

        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            resp.close()
            if not location:
                raise UnsafeURLError(f"{current} redirected without a Location")
            if hop >= max_redirects:
                raise UnsafeURLError(f"too many redirects (>{max_redirects})")
            # Rebuild relative redirects against the current URL, then loop: the
            # next iteration re-runs check_url on the new target. This is the
            # whole point — a public host redirecting to 127.0.0.1 is refused.
            current = requests.compat.urljoin(current, location)
            continue

        if resp.status_code >= 400:
            resp.close()
            raise UnsafeURLError(f"{current} returned HTTP {resp.status_code}")

        declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if declared and declared not in ALLOWED_IMAGE_TYPES:
            resp.close()
            raise UnsafeURLError(
                f"content-type {declared!r} is not an accepted image type"
            )

        # Content-Length is a hint, not a guarantee — enforce the cap while reading.
        declared_length = resp.headers.get("content-length")
        if declared_length and declared_length.isdigit() and int(declared_length) > max_bytes:
            resp.close()
            raise UnsafeURLError(
                f"declared size {int(declared_length)} bytes exceeds the {max_bytes} byte limit"
            )

        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeURLError(
                        f"response exceeded the {max_bytes} byte limit while streaming"
                    )
                chunks.append(chunk)
        finally:
            resp.close()

        return FetchResult(
            url=url, content=b"".join(chunks), content_type=declared,
            final_url=current, size=total,
        )

    raise UnsafeURLError("redirect loop")


def fetch_image(url: str, dest: str | Path, max_bytes: int = DEFAULT_MAX_BYTES,
                max_pixels: int = DEFAULT_MAX_PIXELS, allow_private: bool = False) -> Path:
    """Fetch a remote image, verify it really is one, and save it.

    The decode step matters: a `content-type: image/png` header proves nothing,
    and a decompression bomb is a small file that explodes in memory when decoded.
    """
    from PIL import Image

    result = fetch_bytes(url, max_bytes=max_bytes, allow_private=allow_private)

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        import io

        with Image.open(io.BytesIO(result.content)) as probe:
            probe.verify()                      # catches truncated/corrupt files
        with Image.open(io.BytesIO(result.content)) as probe:
            width, height = probe.size
            if width * height > max_pixels:
                raise UnsafeURLError(
                    f"image is {width}x{height} = {width * height} pixels, "
                    f"above the {max_pixels} pixel budget"
                )
    except UnsafeURLError:
        raise
    except Image.DecompressionBombError as exc:
        # PIL refuses before we can measure: say what the limit is, rather than
        # surfacing its internal "DOS attack" wording to a user.
        raise UnsafeURLError(
            f"image exceeds the {max_pixels} pixel budget (decompression bomb guard)"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not an image"
        raise UnsafeURLError(f"downloaded bytes are not a decodable image: {exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    suffix = Path(urlparse(result.final_url).path).suffix or ".png"
    target = Path(dest)
    if target.suffix.lower() != suffix.lower():
        target = target.with_suffix(suffix)
    return result.save(target)
