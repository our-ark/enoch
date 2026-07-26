from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


STORAGE_API_VERSION = 1
StorageArea = Literal["software-body", "private-state", "artifacts"]


class StorageLayoutError(ValueError):
    """Raised when a storage layout or path crosses an ownership boundary."""


@dataclass(frozen=True)
class StorageLayout:
    """Versioned ownership boundaries for one local agent instance."""

    software_body: Path
    private_state: Path
    artifacts: Path
    api_version: int = STORAGE_API_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "software_body", self.software_body.expanduser().resolve())
        object.__setattr__(self, "private_state", self.private_state.expanduser().resolve())
        object.__setattr__(self, "artifacts", self.artifacts.expanduser().resolve())
        validate_storage_layout(self)

    def body_path(self, relative: str | Path) -> Path:
        candidate = _bounded_path(self.software_body, relative, "software body")
        if _contains(self.private_state, candidate) or _contains(self.artifacts, candidate):
            raise StorageLayoutError(
                f"Software-body path crosses a private storage boundary: {candidate}"
            )
        return candidate

    def private_path(self, relative: str | Path) -> Path:
        candidate = _bounded_path(self.private_state, relative, "private state")
        if _contains(self.artifacts, candidate):
            raise StorageLayoutError(
                f"Private-state path crosses the artifact boundary: {candidate}"
            )
        return candidate

    def artifact_path(self, relative: str | Path) -> Path:
        return _bounded_path(self.artifacts, relative, "artifact")

    def contains(self, area: StorageArea, path: Path) -> bool:
        candidate = path.expanduser().resolve()
        if area == "software-body":
            return (
                _contains(self.software_body, candidate)
                and not _contains(self.private_state, candidate)
                and not _contains(self.artifacts, candidate)
            )
        if area == "private-state":
            return _contains(self.private_state, candidate) and not _contains(
                self.artifacts,
                candidate,
            )
        if area == "artifacts":
            return _contains(self.artifacts, candidate)
        raise StorageLayoutError(f"Unknown storage area: {area}")


def validate_storage_layout(layout: StorageLayout) -> StorageLayout:
    if layout.api_version != STORAGE_API_VERSION:
        raise StorageLayoutError(
            f"Storage layout uses API version {layout.api_version}; "
            f"Enoch supports version {STORAGE_API_VERSION}."
        )
    if layout.software_body == layout.private_state:
        raise StorageLayoutError("Software body and private state must use different roots.")
    if layout.software_body == layout.artifacts:
        raise StorageLayoutError("Software body and artifacts must use different roots.")
    if layout.private_state == layout.artifacts:
        raise StorageLayoutError("Private state and artifacts must use different roots.")
    if _contains(layout.private_state, layout.software_body):
        raise StorageLayoutError("Software body cannot be nested inside private state.")
    if _contains(layout.artifacts, layout.software_body):
        raise StorageLayoutError("Software body cannot be nested inside artifact storage.")
    if _contains(layout.artifacts, layout.private_state):
        raise StorageLayoutError("Private state cannot be nested inside artifact storage.")
    git_metadata = layout.software_body / ".git"
    if _contains(git_metadata, layout.private_state) or _contains(
        git_metadata,
        layout.artifacts,
    ):
        raise StorageLayoutError("Private storage cannot be placed inside Git metadata.")
    return layout


def _bounded_path(base: Path, relative: str | Path, label: str) -> Path:
    value = Path(relative)
    if value.is_absolute():
        raise StorageLayoutError(f"{label.title()} path must be relative: {value}")
    candidate = (base / value).resolve()
    if not _contains(base, candidate):
        raise StorageLayoutError(f"{label.title()} path escapes its boundary: {value}")
    return candidate


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True
