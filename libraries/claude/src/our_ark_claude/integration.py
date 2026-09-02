from __future__ import annotations

from pathlib import Path
import shlex
from typing import Callable

from our_ark_provider_kit import agent_context
from our_ark_claude.core import ClaudeRuntime


def create_provider(root: Path | None = None) -> ClaudeRuntime:
    context = agent_context(root)
    config = context.module("config")
    paths = context.module("paths")
    return ClaudeRuntime(
        root=context.root,
        read_settings=lambda selected_root=None: config.read_section(
            "claude",
            selected_root or context.root,
        ),
        write_setting=lambda key, value, selected_root=None: config.write_section_value(
            "claude",
            key,
            value,
            selected_root or context.root,
        ),
        session_path=paths.private_state_path(
            Path("runtime") / "claude" / "sessions.json",
            context.root,
        ),
        env_prefix=context.env_prefix,
        agent_name=context.name,
    )


def setup_provider(
    text: str,
    root: Path,
    *,
    prompt: Callable[[str], str] | None = None,
    prefix: str = "",
) -> str:
    del prompt
    try:
        args = tuple(shlex.split(text))
    except ValueError:
        args = ("help",)
    return create_provider(root).configure(args, root, prefix=prefix or "/")
