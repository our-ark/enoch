from __future__ import annotations

from pathlib import Path

from enoch.providers.contracts import VersionControlProviderError
from enoch.providers.registry import ProviderError, load_provider


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
    try:
        provider = load_provider("vcs", root)
        if provider.is_clean(root):
            return "Worktree status: clean"
        changed = [str(path) for path in provider.changed_files(root)]
    except (ProviderError, VersionControlProviderError, OSError) as error:
        return f"Worktree status: unknown\n{error}"
    if not changed:
        return "Worktree status: dirty"
    return "\n".join(["Worktree status: dirty", *changed])
