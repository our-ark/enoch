from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import stat
import zipfile

from enoch.config import read_section
from enoch.paths import storage_layout
from enoch.providers.contracts import OutboundAttachment, RuntimeOutputReference, RuntimeResult


DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_OUTBOUND_ATTACHMENTS = 16
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_OFFICE_MARKERS = {
    ".docx": "word/",
    ".xlsx": "xl/",
    ".pptx": "ppt/",
}
_SENSITIVE_NAMES = {".env", ".git", ".ssh", "credentials", "secrets"}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdb", ".keystore"}


class OutboundArtifactError(ValueError):
    """Raised when a runtime output cannot cross the artifact boundary safely."""


@dataclass(frozen=True)
class OutboundCapture:
    attachments: tuple[OutboundAttachment, ...]
    rejected: int = 0


def capture_runtime_attachments(
    result: RuntimeResult,
    root: Path,
) -> OutboundCapture:
    """Import trusted runtime file references into instance artifact storage."""

    candidates = [
        *_reference_candidates(result.output_refs, root),
        *_event_candidates(result, root),
    ]
    attachments: list[OutboundAttachment] = []
    seen: set[tuple[str, str]] = set()
    rejected = 0
    for candidate in candidates:
        if len(attachments) >= MAX_OUTBOUND_ATTACHMENTS:
            rejected += 1
            continue
        try:
            attachment = import_outbound_artifact(candidate, root)
        except (OSError, OutboundArtifactError):
            rejected += 1
            continue
        identity = (attachment.sha256, attachment.filename)
        if identity in seen:
            continue
        seen.add(identity)
        attachments.append(attachment)
    return OutboundCapture(tuple(attachments), rejected)


def import_outbound_artifact(
    source: Path,
    root: Path,
    *,
    max_bytes: int | None = None,
) -> OutboundAttachment:
    source_path = Path(source).expanduser()
    approved_roots = outbound_source_roots(root)
    resolved = _approved_regular_file(source_path, approved_roots)
    limit = max_bytes or outbound_max_bytes(root)
    data = _read_bounded(resolved, limit)
    mime_type = _verified_mime(resolved.name, data)
    digest = hashlib.sha256(data).hexdigest()
    suffix = resolved.suffix.lower()
    relative = Path("outbound") / digest[:2] / f"{digest}{suffix}"
    destination = storage_layout(root).artifact_path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return OutboundAttachment(
        id=f"sha256:{digest}",
        uri=f"artifact://{relative.as_posix()}",
        filename=resolved.name,
        mime_type=mime_type,
        size=len(data),
        sha256=digest,
        kind="image" if mime_type.startswith("image/") else "file",
    )


def outbound_source_roots(root: Path) -> tuple[Path, ...]:
    layout = storage_layout(root)
    configured = read_section("codex", root).get("generated_images_root", "").strip()
    if configured:
        generated = Path(configured).expanduser()
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        generated = codex_home / "generated_images"
    return (layout.artifacts.resolve(), generated.resolve())


def outbound_max_bytes(root: Path) -> int:
    raw = read_section("outbound", root).get("max_attachment_bytes", "").strip()
    if not raw:
        return DEFAULT_MAX_ATTACHMENT_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_ATTACHMENT_BYTES
    return value if 1 <= value <= 100 * 1024 * 1024 else DEFAULT_MAX_ATTACHMENT_BYTES


def artifact_relative_path(uri: str) -> Path:
    if not uri.startswith("artifact://"):
        raise OutboundArtifactError("Attachment does not use an artifact URI.")
    value = uri.removeprefix("artifact://")
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or value != pure.as_posix()
        or ".." in pure.parts
        or "." in pure.parts
    ):
        raise OutboundArtifactError("Attachment artifact URI is invalid.")
    if any(part.startswith(".") for part in pure.parts):
        raise OutboundArtifactError("Hidden artifact paths cannot be attached.")
    return Path(*pure.parts)


def _reference_candidates(
    references: tuple[RuntimeOutputReference, ...],
    root: Path,
) -> tuple[Path, ...]:
    result: list[Path] = []
    artifacts = storage_layout(root).artifacts
    for reference in references:
        kind = reference.kind.strip().lower()
        uri = reference.uri.strip()
        if kind not in {"artifact", "file", "image"}:
            continue
        if uri.startswith("artifact://"):
            try:
                result.append(artifacts / artifact_relative_path(uri))
            except OutboundArtifactError:
                continue
        elif uri.startswith("file://"):
            result.append(Path(uri.removeprefix("file://")))
        elif Path(uri).is_absolute():
            result.append(Path(uri))
    return tuple(result)


def _event_candidates(result: RuntimeResult, root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for event in result.events:
        if event.type not in {"item.completed", "item_completed"}:
            continue
        item = event.data.get("item")
        if not isinstance(item, dict):
            payload = event.data.get("payload")
            item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        kind = str(item.get("kind") or "").strip().lower()
        candidate = ""
        if item_type == "extension" and kind == "image_gen.generation":
            candidate = str(item.get("savedPath") or item.get("saved_path") or "")
        elif item_type in {"imageview", "image_view"}:
            candidate = str(item.get("path") or "")
        if candidate.startswith("file://"):
            candidate = candidate.removeprefix("file://")
        if candidate:
            paths.append(Path(candidate))
    return tuple(paths)


def _approved_regular_file(path: Path, roots: tuple[Path, ...]) -> Path:
    if not path.is_absolute():
        raise OutboundArtifactError("Attachment source must be absolute.")
    if ".." in path.parts:
        raise OutboundArtifactError("Attachment source contains path traversal.")
    lexical = Path(os.path.abspath(path))
    approved_root = next(
        (base for base in roots if lexical == base or base in lexical.parents),
        None,
    )
    if approved_root is None:
        raise OutboundArtifactError("Attachment source is outside approved roots.")
    relative = lexical.relative_to(approved_root)
    current = approved_root
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise OutboundArtifactError("Symbolic links cannot be attached.")
        except FileNotFoundError as error:
            raise OutboundArtifactError("Attachment source does not exist.") from error
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise OutboundArtifactError("Attachment source does not exist.") from error
    if resolved != lexical or not (resolved == approved_root or approved_root in resolved.parents):
        raise OutboundArtifactError("Attachment source is outside approved roots.")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OutboundArtifactError("Attachment source is not a regular file.")
    _reject_sensitive_name(resolved)
    return resolved


def _reject_sensitive_name(path: Path) -> None:
    lowered = tuple(part.lower() for part in path.parts)
    if any(part in _SENSITIVE_NAMES for part in lowered):
        raise OutboundArtifactError("Sensitive files cannot be attached.")
    stem = path.stem.lower()
    if (
        path.name.startswith(".")
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
        or stem in {"secret", "secrets", "credential", "credentials", "token", "tokens"}
        or stem.startswith(".env")
    ):
        raise OutboundArtifactError("Sensitive files cannot be attached.")


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OutboundArtifactError("Attachment source is not a regular file.")
        if info.st_size < 1 or info.st_size > max_bytes:
            raise OutboundArtifactError("Attachment size is outside the allowed limit.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            raise OutboundArtifactError("Attachment size is outside the allowed limit.")
        return data
    finally:
        os.close(descriptor)


def _verified_mime(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    expected = _IMAGE_TYPES.get(suffix) or _DOCUMENT_TYPES.get(suffix)
    if expected is None:
        raise OutboundArtifactError("Attachment type is not allowed.")
    if _looks_sensitive_content(data):
        raise OutboundArtifactError("Sensitive file content cannot be attached.")
    valid = False
    if expected == "image/png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")
    elif expected == "image/jpeg":
        valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    elif expected == "image/webp":
        valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    elif expected == "image/gif":
        valid = data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";")
    elif expected == "application/pdf":
        valid = data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]
    elif suffix in _OFFICE_MARKERS:
        valid = _valid_office_zip(data, _OFFICE_MARKERS[suffix])
    elif suffix == ".json":
        try:
            json.loads(data.decode("utf-8"))
            valid = True
        except (UnicodeDecodeError, json.JSONDecodeError):
            valid = False
    else:
        try:
            data.decode("utf-8")
            valid = b"\x00" not in data
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise OutboundArtifactError("Attachment content does not match its extension and MIME type.")
    return expected


def _looks_sensitive_content(data: bytes) -> bool:
    sample = data[:262_144]
    markers = (
        b"-----BEGIN PRIVATE KEY-----",
        b"-----BEGIN RSA PRIVATE KEY-----",
        b"-----BEGIN EC PRIVATE KEY-----",
        b"-----BEGIN OPENSSH PRIVATE KEY-----",
        b"xoxb-",
        b"xapp-",
        b"ghp_",
    )
    return any(marker in sample for marker in markers)


def _valid_office_zip(data: bytes, marker: str) -> bool:
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            return "[Content_Types].xml" in names and any(name.startswith(marker) for name in names)
    except (OSError, zipfile.BadZipFile):
        return False
