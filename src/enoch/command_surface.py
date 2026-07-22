from __future__ import annotations

from pathlib import Path

from enoch.git_tools import run_git
from enoch.providers.registry import load_provider, provider_name


def lineage_usage(prefix: str = "", *, command_name: str = "ancestors") -> str:
    command = f"{prefix}{command_name}"
    if command_name == "inherit":
        return "\n".join(
            [
                "Inherit commands:",
                f"{command} - show inheritable direct-parent changes",
                f"{command} show - show inheritable direct-parent changes",
                f"{command} <change_id> - inherit one direct-parent change",
                f"{command} all - inherit all direct-parent changes",
                f"{command} ignore <candidate> - hide a change",
            ]
        )
    return "\n".join(
        [
            "Ancestor commands:",
            f"{command} - show the ancestor chain, ancestor skills, and pending change counts",
            f"Use {prefix}inherit to show direct-parent changes.",
            f"Use {prefix}inherit <change_id> to inherit one direct-parent change.",
        ]
    )


def checktree(root: Path | None = None) -> str:
    selected = provider_name("vcs", root)
    provider = load_provider("vcs", root, name=selected)
    if selected != "git" and hasattr(provider, "is_clean"):
        return f"Worktree status: {'clean' if provider.is_clean(root) else 'dirty'}"
    result = run_git(["status", "--porcelain"], root)
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "Could not inspect worktree."
        return f"Worktree status: unknown\n{detail}"
    if not result.stdout:
        return "Worktree status: clean"
    return "\n".join(["Worktree status: dirty", result.stdout])
