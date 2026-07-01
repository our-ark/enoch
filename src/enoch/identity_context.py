from __future__ import annotations

from pathlib import Path

from enoch.identity import Identity
from enoch.lineage.core import load_parent


def display_ancestor(identity: Identity, root: Path | None = None) -> str:
    parent = load_parent(root)
    if parent is not None:
        return parent.name
    return identity.ancestor or "none"
