from __future__ import annotations

import json
from pathlib import Path

from enoch.git_tools import run_git
from enoch.paths import enoch_home


ACTION_MODE_FULL_ACCESS = "full-access"
ACTION_MODE_CONVERSATION = "conversation-only"
ACTION_MODES = {ACTION_MODE_FULL_ACCESS, ACTION_MODE_CONVERSATION}


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
    result = run_git(["status", "--porcelain"], root)
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "Could not inspect worktree."
        return f"Worktree status: unknown\n{detail}"
    if not result.stdout:
        return "Worktree status: clean"
    return "\n".join(["Worktree status: dirty", result.stdout])


def action_mode_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "action_mode.json"


def action_mode(root: Path | None = None) -> str:
    path = action_mode_path(root)
    if not path.exists():
        return ACTION_MODE_FULL_ACCESS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ACTION_MODE_FULL_ACCESS
    mode = str(data.get("mode") or "").strip()
    return mode if mode in ACTION_MODES else ACTION_MODE_FULL_ACCESS


def save_action_mode(mode: str, root: Path | None = None) -> None:
    if mode not in ACTION_MODES:
        raise ValueError(f"Invalid Enoch action mode: {mode}")
    path = action_mode_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode}, indent=2), encoding="utf-8")


def action_mode_label(mode: str) -> str:
    if mode == ACTION_MODE_CONVERSATION:
        return "conversation-only"
    return "full-access"


def action_mode_description(mode: str) -> str:
    if mode == ACTION_MODE_CONVERSATION:
        return (
            "Enoch can talk and discuss changes, but will not edit code, push, or open PRs. "
            "Codex action runs are restricted to read-only."
        )
    return "Enoch can run repository implementation work with Codex danger-full-access."
