from __future__ import annotations

from pathlib import Path


def repo_root(start: Path | None = None) -> Path:
    path = (start or Path.cwd()).resolve()
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return path


def enoch_home(root: Path | None = None) -> Path:
    return repo_root(root) / ".enoch"
