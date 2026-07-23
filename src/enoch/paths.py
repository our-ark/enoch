from __future__ import annotations

import os
from pathlib import Path


def repo_root(start: Path | None = None) -> Path:
    path = (start or Path.cwd()).resolve()
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return path


def enoch_home(root: Path | None = None) -> Path:
    resolved_root = repo_root(root)
    redirected_root = os.environ.get("ENOCH_STATE_REDIRECT_ROOT", "").strip()
    redirected_home = os.environ.get("ENOCH_STATE_HOME", "").strip()
    if redirected_root and redirected_home:
        try:
            matches = resolved_root == Path(redirected_root).expanduser().resolve()
        except OSError:
            matches = False
        if matches:
            return Path(redirected_home).expanduser().resolve()
    return resolved_root / ".enoch"
