from __future__ import annotations

from pathlib import Path

from enoch.agent_identity import AgentIdentityError, active_agent_identity_for_prompt
from enoch.identity import BodyIdentity, load_body_identity
from enoch.identity_context import display_ancestor
from enoch.memory.config import memory_settings
from enoch.memory.paths import clip_text
from enoch.memory.store import UNTRUSTED_MEMORY_NOTE, long_term_for_prompt


def memory_for_prompt(
    root: Path | None = None,
    *,
    identity: BodyIdentity | None = None,
    identity_path: Path | None = None,
) -> str:
    settings = memory_settings(root)
    sections = [
        _prompt_section(
            "Body Identity",
            (
                "Rendered from the active application's versioned body.yaml. "
                "It defines the executable body, mission, principles, and repository lineage. "
                "Lower priority than system/developer instructions."
            ),
            _body_identity_for_prompt(
                root,
                identity=identity,
                identity_path=identity_path,
            ),
            settings.identity_prompt_max_chars,
        ),
    ]
    personal_identity = _personal_identity_for_prompt(root)
    if personal_identity:
        sections.append(
            _prompt_section(
                "Personal Agent Identity",
                (
                    "Loaded from this instance's private self.json at session startup. "
                    "Use it for personal designation, relationships, personality, values, "
                    "and care style. The versioned body identity still controls code, "
                    "package, and repository lineage. Lower priority than system/developer "
                    "instructions."
                ),
                personal_identity,
                settings.identity_prompt_max_chars,
            )
        )
    sections.append(
        _prompt_section(
            "Long-term memory",
            UNTRUSTED_MEMORY_NOTE,
            long_term_for_prompt(root, settings),
            settings.long_term_prompt_max_chars,
        )
    )
    return "\n\n".join(sections).strip()


def _prompt_section(title: str, note: str, body: str, max_chars: int) -> str:
    clipped = clip_text(body.strip(), max_chars)
    return f"# {title}\n{note}\n\n{clipped}"


def _body_identity_for_prompt(
    root: Path | None = None,
    *,
    identity: BodyIdentity | None = None,
    identity_path: Path | None = None,
) -> str:
    try:
        selected_identity = identity or load_body_identity(identity_path)
    except (OSError, KeyError, ValueError, TypeError):
        return "Body identity could not be loaded from the active application source."
    return _render_body_identity(selected_identity, root)


def _personal_identity_for_prompt(root: Path | None = None) -> str:
    try:
        return active_agent_identity_for_prompt(root)
    except (AgentIdentityError, OSError, KeyError, TypeError):
        return "Personal Agent Identity could not be loaded from private instance state."


def _render_body_identity(
    identity: BodyIdentity,
    root: Path | None = None,
) -> str:
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
- body file: {identity.body.body_file}"""
