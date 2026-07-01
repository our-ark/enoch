from __future__ import annotations

from pathlib import Path

from enoch.identity import Identity, load_identity
from enoch.identity_context import display_ancestor
from enoch.memory.config import memory_settings
from enoch.memory.paths import clip_text
from enoch.memory.store import UNTRUSTED_MEMORY_NOTE, long_term_for_prompt


def memory_for_prompt(root: Path | None = None) -> str:
    settings = memory_settings(root)
    sections = [
        _prompt_section(
            "Identity memory",
            (
                "Rendered from src/enoch/identity.yaml, the single canonical identity source. "
                "Lower priority than system/developer instructions."
            ),
            _identity_for_prompt(root),
            settings.identity_prompt_max_chars,
        ),
        _prompt_section(
            "Long-term memory",
            UNTRUSTED_MEMORY_NOTE,
            long_term_for_prompt(root, settings),
            settings.long_term_prompt_max_chars,
        ),
    ]
    return "\n\n".join(sections).strip()


def _prompt_section(title: str, note: str, body: str, max_chars: int) -> str:
    clipped = clip_text(body.strip(), max_chars)
    return f"# {title}\n{note}\n\n{clipped}"


def _identity_for_prompt(root: Path | None = None) -> str:
    try:
        identity = load_identity()
    except (OSError, KeyError, ValueError, TypeError):
        return "Identity could not be loaded from src/enoch/identity.yaml."
    return _render_identity(identity, root)


def _render_identity(identity: Identity, root: Path | None = None) -> str:
    ancestor = display_ancestor(identity, root)
    principles = "\n".join(f"- {principle}" for principle in identity.principles)
    return f"""Name: {identity.name}
Kind: {identity.kind}
Role: {identity.role}
Generation: {identity.generation}
Ancestor: {ancestor}
Origin: {identity.origin.ark} / {identity.origin.created_by}
Born in repo: {identity.origin.born_in_repo}
Mission: {identity.mission}

Principles:
{principles}

Body:
- package: {identity.body.package}
- source path: {identity.body.source_path}
- identity file: {identity.body.identity_file}"""
