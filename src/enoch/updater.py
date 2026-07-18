from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

from enoch.evolve_lifecycle import (
    promotions_pending_adoption,
    stage_promoted_evolve_adoptions,
)
from enoch.formatting import format_doctor_result
from enoch.git_tools import GitError, current_branch, ensure_clean_worktree
from enoch.immune import run_immune_system
from enoch.paths import enoch_home
from enoch.runtime import DEFAULT_BRANCH, DEFAULT_REMOTE
from enoch.update_tools import (
    current_head,
    fetch_origin_main,
    head_merged_into_origin_main,
    pull_origin_main,
    reset_hard,
)


@dataclass(frozen=True)
class UpdateResult:
    message: str
    direct_action_result: str
    restart_required: bool = False


def update_from_main(root: Path) -> UpdateResult:
    try:
        ensure_clean_worktree(root)
        fetch_origin_main(root)
        branch = current_branch(root)
        if branch != DEFAULT_BRANCH:
            if not branch:
                return _message(f"Enoch could not update: current checkout is detached. Switch to {DEFAULT_BRANCH} first.")
            if not head_merged_into_origin_main(root):
                return _message(
                    f"Enoch could not update: current branch {branch} has commits that are not merged into "
                    f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}. Finish, merge, or switch branches first."
                )
        previous_head = current_head(root)
        pull_result = pull_origin_main(root)
        updated_head = current_head(root)
    except GitError as error:
        return _message(f"Enoch could not update: {error}")

    if previous_head == updated_head:
        pending_promotions = promotions_pending_adoption(root, updated_head)
        if pending_promotions:
            doctor = run_immune_system(root)
            if not doctor.passed:
                return _message(
                    "\n\n".join(
                        [
                            "Enoch is already up to date, but adoption verification failed.",
                            format_doctor_result(doctor),
                            "No adoption event was staged.",
                        ]
                    )
                )
            staged_note = _stage_adoptions(root, updated_head)
            formatted_doctor = format_doctor_result(doctor)
            return UpdateResult(
                message="\n\n".join(
                    part
                    for part in [
                        "Enoch is already up to date and adoption checks passed.",
                        formatted_doctor,
                        staged_note,
                        "Restarting now so the running instance can verify adoption.",
                    ]
                    if part
                ),
                direct_action_result="\n\n".join(
                    part
                    for part in [
                        pull_result,
                        formatted_doctor,
                        staged_note,
                        f"Restarting into {updated_head[:7]}.",
                    ]
                    if part
                ),
                restart_required=True,
            )
        restart_note = _running_commit_restart_note(root, updated_head)
        return UpdateResult(
            message="\n\n".join(part for part in ["Enoch is already up to date.", restart_note] if part),
            direct_action_result="\n\n".join(part for part in [pull_result, restart_note] if part),
        )

    doctor = run_immune_system(root)
    if not doctor.passed:
        try:
            reset_hard(previous_head, root)
            rollback = f"Rolled back to {previous_head[:7]}."
        except GitError as error:
            rollback = f"Rollback failed: {error}"
        return _message(
            "\n\n".join(
                [
                    "Enoch pulled latest main, but doctor failed. I am not restarting.",
                    format_doctor_result(doctor),
                    rollback,
                    "The currently running Enoch process is still the pre-update code.",
                ]
            )
        )

    formatted_doctor = format_doctor_result(doctor)
    staged_note = _stage_adoptions(root, updated_head)
    return UpdateResult(
        message="\n\n".join(
            part
            for part in [
                "Enoch pulled latest main and doctor passed.",
                formatted_doctor,
                staged_note,
                "Restarting now. The startup notification will confirm Enoch came back.",
            ]
            if part
        ),
        direct_action_result="\n\n".join(
            part
            for part in [
                pull_result,
                formatted_doctor,
                staged_note,
                f"Restarting into {updated_head[:7]}.",
            ]
            if part
        ),
        restart_required=True,
    )


def _message(message: str) -> UpdateResult:
    return UpdateResult(message=message, direct_action_result="")


def _stage_adoptions(root: Path, version: str) -> str:
    try:
        staged = stage_promoted_evolve_adoptions(
            root,
            version,
            health_check="passed",
        )
    except OSError as error:
        return f"Could not stage evolution adoption evidence: {error}"
    if not staged:
        return ""
    return f"Staged {len(staged)} promoted evolution(s) for verified adoption after restart."


def _running_commit_restart_note(root: Path, current: str) -> str:
    lifecycle = _load_telegram_lifecycle_state(root)
    if str(lifecycle.get("status") or "") != "running":
        return ""
    if _int(lifecycle.get("pid")) != os.getpid():
        return ""
    started_head = str(lifecycle.get("started_head") or "").strip()
    if not started_head or started_head == current:
        return ""
    return "\n".join(
        [
            f"Local code is current at {current[:7]}, but this Telegram daemon started on {started_head[:7]}.",
            "Run /restart to load the current code.",
        ]
    )


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _load_telegram_lifecycle_state(root: Path) -> dict:
    path = enoch_home(root) / "telegram_lifecycle.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
