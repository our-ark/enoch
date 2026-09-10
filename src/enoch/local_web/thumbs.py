from __future__ import annotations

from html import unescape
import json
from pathlib import Path
import os
import re
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from enoch.local_web.shortlist import ShopProduct, ShopShortlist, load_shortlist
from enoch.paths import artifact_path
from enoch.state import atomic_write, load_json_object


_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
}
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r'([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>=]+)))?',
    re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_SNIFF = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
)
_MAX_HTML_BYTES = 1_000_000
_MAX_IMAGE_BYTES = 1_500_000
_FETCH_TIMEOUT = 8
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def extract_preview_image_url(html: str, page_url: str) -> str:
    for tag in _META_TAG_RE.findall(html):
        attrs = _meta_attrs(tag)
        prop = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = unescape((attrs.get("content") or "").strip())
        if prop in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}:
            resolved = _absolute_http_url(content, page_url)
            if resolved:
                return resolved
    for match in _JSON_LD_RE.finditer(html):
        found = _image_from_json_ld(match.group(1), page_url)
        if found:
            return found
    return ""


def cached_thumbnail(
    shortlist_id: str,
    index: int,
    *,
    root: Path | None = None,
) -> tuple[bytes, str] | None:
    shortlist = load_shortlist(shortlist_id, root=root)
    if shortlist is None:
        return None
    product = next((item for item in shortlist.products if item.index == index), None)
    if product is None:
        return None
    key = f"{shortlist.id}:{product.index}"
    with _lock_for(key):
        cached = _read_cached(shortlist, product, root=root)
        if cached is not None:
            return cached
        return _fetch_and_store(shortlist, product, root=root)


def _fetch_and_store(
    shortlist: ShopShortlist,
    product: ShopProduct,
    *,
    root: Path | None,
) -> tuple[bytes, str] | None:
    html = _download(product.url, accept="text/html,application/xhtml+xml")
    if not html:
        return None
    image_url = extract_preview_image_url(
        html.decode("utf-8", errors="ignore"),
        product.url,
    )
    if not image_url:
        return None
    image = _download(
        image_url,
        accept="image/avif,image/webp,image/*,*/*;q=0.8",
        referer=product.url,
    )
    if not image:
        return None
    content_type, suffix = _image_kind(image)
    if not suffix:
        return None
    meta_path, bin_path = _thumb_paths(shortlist.id, product.index, suffix, root)
    _atomic_write_bytes(bin_path, image)
    atomic_write(
        meta_path,
        json.dumps(
            {
                "product_url": product.url,
                "image_url": image_url,
                "content_type": content_type,
                "suffix": suffix,
            },
            indent=2,
        )
        + "\n",
    )
    return image, content_type


def _read_cached(
    shortlist: ShopShortlist,
    product: ShopProduct,
    *,
    root: Path | None,
) -> tuple[bytes, str] | None:
    meta_path = _thumb_meta_path(shortlist.id, product.index, root)
    if not meta_path.exists():
        return None
    meta = load_json_object(meta_path)
    if str(meta.get("product_url") or "") != product.url:
        return None
    suffix = str(meta.get("suffix") or "")
    content_type = str(meta.get("content_type") or "")
    bin_path = _thumb_bin_path(shortlist.id, product.index, suffix, root)
    if not suffix or not bin_path.exists():
        return None
    try:
        data = bin_path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return data, content_type or "image/jpeg"


def _thumb_dir(shortlist_id: str, root: Path | None) -> Path:
    return artifact_path(Path("shop") / "thumbs" / shortlist_id, root)


def _thumb_meta_path(shortlist_id: str, index: int, root: Path | None) -> Path:
    return _thumb_dir(shortlist_id, root) / f"{index}.json"


def _thumb_bin_path(
    shortlist_id: str,
    index: int,
    suffix: str,
    root: Path | None,
) -> Path:
    cleaned = suffix if suffix.startswith(".") else f".{suffix}"
    return _thumb_dir(shortlist_id, root) / f"{index}{cleaned}"


def _thumb_paths(
    shortlist_id: str,
    index: int,
    suffix: str,
    root: Path | None,
) -> tuple[Path, Path]:
    return (
        _thumb_meta_path(shortlist_id, index, root),
        _thumb_bin_path(shortlist_id, index, suffix, root),
    )


def _download(url: str, *, accept: str, referer: str = "") -> bytes:
    if not _public_http_url(url):
        return b""
    headers = {**_FETCH_HEADERS, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    request = Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urlopen(request, timeout=_FETCH_TIMEOUT) as response:
            limit = _MAX_HTML_BYTES if "html" in accept else _MAX_IMAGE_BYTES
            data = _read_limited(
                response,
                limit,
                keep_prefix="html" in accept,
            )
            final_url = str(getattr(response, "url", url) or url)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return b""
    if not _public_http_url(final_url):
        return b""
    return data


def _read_limited(
    response: object,
    limit: int,
    *,
    keep_prefix: bool = False,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65_536)  # type: ignore[attr-defined]
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            if not keep_prefix:
                return b""
            extra = total - limit
            keep = len(chunk) - extra
            if keep > 0:
                chunks.append(chunk[:keep])
            return b"".join(chunks)
        chunks.append(chunk)
    return b"".join(chunks)


def _image_kind(data: bytes) -> tuple[str, str]:
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif", ".avif"
    for magic, content_type, suffix in _SNIFF:
        if data.startswith(magic):
            if content_type == "image/webp" and b"WEBP" not in data[:16]:
                continue
            return content_type, suffix
    return "", ""


def _meta_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag[5:]):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[name] = value
    return attrs


def _image_from_json_ld(raw: str, page_url: str) -> str:
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError:
        return ""
    return _first_image_value(payload, page_url)


def _first_image_value(payload: object, page_url: str) -> str:
    if isinstance(payload, str):
        return _absolute_http_url(payload, page_url)
    if isinstance(payload, list):
        for item in payload:
            found = _first_image_value(item, page_url)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("image", "thumbnailUrl", "contentUrl"):
        found = _first_image_value(payload.get(key), page_url)
        if found:
            return found
    types = payload.get("@type")
    type_names = types if isinstance(types, list) else [types]
    if "ImageObject" in type_names:
        return _absolute_http_url(str(payload.get("url") or ""), page_url)
    return ""


def _absolute_http_url(value: str, page_url: str) -> str:
    cleaned = unescape(value.strip())
    if not cleaned or cleaned.startswith("data:"):
        return ""
    resolved = urljoin(page_url, cleaned)
    return resolved if _public_http_url(resolved) else ""


def _public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return False
    return True


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock
