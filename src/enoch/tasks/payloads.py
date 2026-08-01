from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import re
from typing import TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

EXTENSION_METADATA_MAX_BYTES = 16 * 1024
EXTENSION_METADATA_MAX_DEPTH = 5
EXTENSION_METADATA_MAX_ITEMS = 256
EXTENSION_METADATA_MAX_MEMBERS = 64
EXTENSION_METADATA_MAX_STRING_CHARS = 2048
EXTENSION_ARTIFACT_REFERENCE_LIMIT = 64

_METADATA_KEY = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_ARTIFACT_KIND = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}"
)
_EXTENSION_CONTEXT_SOURCE = re.compile(
    r"extension:[a-z0-9][a-z0-9._-]{0,63}"
)
_EXTENSION_LANE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RESERVED_METADATA_KEYS = {"enoch", "schema_version", "system"}
_RESERVED_METADATA_PREFIXES = ("$", "_", "enoch.", "system.")


@dataclass(frozen=True)
class ExtensionArtifactReference:
    """A typed path beneath the originating extension's artifact root."""

    kind: str
    path: str
    media_type: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise ValueError("Extension artifact reference kind must be a string.")
        kind = self.kind.strip().lower()
        if not _ARTIFACT_KIND.fullmatch(kind):
            raise ValueError(
                f"Invalid extension artifact reference kind {self.kind!r}."
            )
        path = _artifact_path(self.path)
        if not isinstance(self.media_type, str):
            raise ValueError(
                "Extension artifact reference media type must be a string."
            )
        media_type = self.media_type.strip().lower()
        if media_type and not _MEDIA_TYPE.fullmatch(media_type):
            raise ValueError(
                f"Invalid extension artifact media type {self.media_type!r}."
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "media_type", media_type)


def normalize_extension_metadata(value: object) -> dict[str, JsonValue]:
    """Return a bounded JSON-safe copy of extension request metadata."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Extension task metadata must be a JSON object.")
    counter = [0]
    normalized = _normalize_json_object(value, depth=1, counter=counter)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > EXTENSION_METADATA_MAX_BYTES:
        raise ValueError(
            "Extension task metadata must be 16384 UTF-8 bytes or fewer."
        )
    return normalized


def normalize_extension_artifact_references(
    values: object,
) -> tuple[ExtensionArtifactReference, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise ValueError(
            "Extension artifact references must be an iterable of typed references."
        )
    try:
        references = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(
            "Extension artifact references must be an iterable of typed references."
        ) from error
    if len(references) > EXTENSION_ARTIFACT_REFERENCE_LIMIT:
        raise ValueError(
            "Extension tasks support at most 64 artifact references."
        )
    if any(
        not isinstance(reference, ExtensionArtifactReference)
        for reference in references
    ):
        raise ValueError(
            "Extension artifact references must be ExtensionArtifactReference values."
        )
    typed = tuple(references)
    identities = tuple((item.kind, item.path, item.media_type) for item in typed)
    if len(set(identities)) != len(identities):
        raise ValueError("Extension artifact references must be unique.")
    return typed


def extension_artifact_references_to_json(
    references: tuple[ExtensionArtifactReference, ...],
) -> list[dict[str, str]]:
    return [asdict(reference) for reference in references]


def extension_artifact_references_from_json(
    value: object,
) -> tuple[ExtensionArtifactReference, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Persisted extension artifact references must be a list.")
    references = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError(
                "Persisted extension artifact references must be objects."
            )
        if set(raw) - {"kind", "path", "media_type"}:
            raise ValueError(
                "Persisted extension artifact references contain unknown fields."
            )
        references.append(
            ExtensionArtifactReference(
                kind=raw.get("kind"),  # type: ignore[arg-type]
                path=raw.get("path"),  # type: ignore[arg-type]
                media_type=raw.get("media_type", ""),  # type: ignore[arg-type]
            )
        )
    return normalize_extension_artifact_references(references)


def require_extension_payload_namespace(
    context_source: object,
    metadata: dict[str, JsonValue],
    artifact_refs: tuple[ExtensionArtifactReference, ...],
) -> None:
    if not metadata and not artifact_refs:
        return
    if (
        not isinstance(context_source, str)
        or not _EXTENSION_CONTEXT_SOURCE.fullmatch(context_source)
    ):
        raise ValueError(
            "Extension task metadata and artifact references require "
            "context_source=extension:<name>."
        )


def normalize_extension_lane(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Extension execution lane must be a string.")
    lane = value.strip().lower()
    if lane and not _EXTENSION_LANE.fullmatch(lane):
        raise ValueError(f"Invalid extension execution lane {value!r}.")
    return lane


def scoped_extension_lane(extension_name: str, lane: object) -> str:
    context_source = f"extension:{extension_name}"
    if not _EXTENSION_CONTEXT_SOURCE.fullmatch(context_source):
        raise ValueError(f"Invalid extension name {extension_name!r}.")
    normalized = normalize_extension_lane(lane)
    return f"{context_source}:{normalized}" if normalized else ""


def require_extension_lane_namespace(
    context_source: object,
    execution_lane: object,
) -> str:
    if execution_lane in (None, ""):
        return ""
    if not isinstance(context_source, str) or not isinstance(execution_lane, str):
        raise ValueError(
            "Extension execution lanes require context_source=extension:<name>."
        )
    prefix = f"{context_source}:"
    if (
        not _EXTENSION_CONTEXT_SOURCE.fullmatch(context_source)
        or not execution_lane.startswith(prefix)
        or scoped_extension_lane(
            context_source.removeprefix("extension:"),
            execution_lane.removeprefix(prefix),
        )
        != execution_lane
    ):
        raise ValueError(
            "Extension execution lanes must remain within the originating "
            "extension namespace."
        )
    return execution_lane


def local_extension_lane(extension_name: str, execution_lane: object) -> str:
    canonical = require_extension_lane_namespace(
        f"extension:{extension_name}",
        execution_lane,
    )
    if not canonical:
        return ""
    return canonical.removeprefix(f"extension:{extension_name}:")


def _normalize_json_object(
    value: dict[object, object],
    *,
    depth: int,
    counter: list[int],
) -> dict[str, JsonValue]:
    _check_depth(depth)
    if len(value) > EXTENSION_METADATA_MAX_MEMBERS:
        raise ValueError(
            "Extension task metadata objects support at most 64 members."
        )
    normalized: dict[str, JsonValue] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise ValueError("Extension task metadata keys must be strings.")
        key = raw_key
        if not _METADATA_KEY.fullmatch(key):
            raise ValueError(f"Invalid extension task metadata key {raw_key!r}.")
        if key in _RESERVED_METADATA_KEYS or key.startswith(
            _RESERVED_METADATA_PREFIXES
        ):
            raise ValueError(
                f"Extension task metadata key {raw_key!r} is reserved."
            )
        normalized[key] = _normalize_json_value(
            raw_value,
            depth=depth + 1,
            counter=counter,
        )
    return normalized


def _normalize_json_value(
    value: object,
    *,
    depth: int,
    counter: list[int],
) -> JsonValue:
    _check_depth(depth)
    counter[0] += 1
    if counter[0] > EXTENSION_METADATA_MAX_ITEMS:
        raise ValueError(
            "Extension task metadata supports at most 256 values."
        )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Extension task metadata numbers must be finite.")
        return value
    if isinstance(value, str):
        if len(value) > EXTENSION_METADATA_MAX_STRING_CHARS:
            raise ValueError(
                "Extension task metadata strings must be 2048 characters or fewer."
            )
        return value
    if isinstance(value, list):
        if len(value) > EXTENSION_METADATA_MAX_MEMBERS:
            raise ValueError(
                "Extension task metadata arrays support at most 64 values."
            )
        return [
            _normalize_json_value(item, depth=depth + 1, counter=counter)
            for item in value
        ]
    if isinstance(value, dict):
        return _normalize_json_object(value, depth=depth, counter=counter)
    raise ValueError(
        "Extension task metadata values must be JSON null, booleans, numbers, "
        "strings, arrays, or objects."
    )


def _check_depth(depth: int) -> None:
    if depth > EXTENSION_METADATA_MAX_DEPTH:
        raise ValueError(
            "Extension task metadata supports at most 5 nested levels."
        )


def _artifact_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Extension artifact reference path must be a string.")
    path = value.strip()
    if (
        not path
        or len(path) > 512
        or path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or "://" in path
    ):
        raise ValueError(
            "Extension artifact reference paths must be relative POSIX paths "
            "of 512 characters or fewer."
        )
    segments = path.split("/")
    if (
        any(
            not segment or segment in {".", ".."} or len(segment) > 128
            for segment in segments
        )
        or segments[0].lower() == "extensions"
    ):
        raise ValueError(
            "Extension artifact reference paths must remain within the "
            "originating extension artifact namespace."
        )
    return "/".join(segments)
