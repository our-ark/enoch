from __future__ import annotations

from pathlib import Path

from enoch.storage import StorageLayout, local_storage_layout


def repo_root(start: Path | None = None) -> Path:
    path = (start or Path.cwd()).resolve()
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return path


def storage_layout(root: Path | None = None) -> StorageLayout:
    return local_storage_layout(repo_root(root))


def enoch_home(root: Path | None = None) -> Path:
    """Compatibility alias for the private-state root."""

    return storage_layout(root).private_state


def private_state_path(relative: str | Path, root: Path | None = None) -> Path:
    return storage_layout(root).private_path(relative)


def artifact_path(relative: str | Path, root: Path | None = None) -> Path:
    return storage_layout(root).artifact_path(relative)


def software_body_path(relative: str | Path, root: Path | None = None) -> Path:
    return storage_layout(root).body_path(relative)


def legacy_artifact_path(relative: str | Path, root: Path | None = None) -> Path:
    """Return the pre-storage-boundary location for read compatibility only."""

    return storage_layout(root).private_path(relative)


def artifact_read_paths(relative: str | Path, root: Path | None = None) -> tuple[Path, ...]:
    current = artifact_path(relative, root)
    legacy = legacy_artifact_path(relative, root)
    return (legacy, current) if legacy != current else (current,)
