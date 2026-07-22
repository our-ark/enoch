from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

from enoch.channel import load_channel_lifecycle, provider_label
from enoch.evolution.lifecycle import (
    promotions_pending_adoption,
    stage_promoted_evolve_adoptions,
)
from enoch.formatting import format_doctor_result
from enoch.vcs_tools import VcsError, current_branch, ensure_clean_worktree
from enoch.immune import DoctorCheckResult, DoctorDiagnosis, ImmuneResult
from enoch.providers.registry import provider_name
from enoch.operations.update_tools import (
    authoritative_branch_name,
    current_repository_revision,
    current_revision_on_authoritative,
    refresh_repository,
    restore_repository_revision,
    update_repository,
)


@dataclass(frozen=True)
class UpdateResult:
    message: str
    direct_action_result: str
    restart_required: bool = False


UPDATE_DOCTOR_TIMEOUT_SECONDS = 300


def update_from_authoritative(root: Path) -> UpdateResult:
    try:
        ensure_clean_worktree(root)
        authoritative = authoritative_branch_name(root)
        refresh_repository(root)
        branch = current_branch(root)
        if branch != authoritative:
            if not branch:
                return _message(
                    "Enoch could not update: current checkout is detached. "
                    f"Switch to {authoritative} first."
                )
            if not current_revision_on_authoritative(root):
                return _message(
                    f"Enoch could not update: current branch {branch} has commits that are not merged into "
                    f"the authoritative {authoritative} branch. Finish, merge, or switch branches first."
                )
        previous_head = current_repository_revision(root)
        pull_result = update_repository(root)
        updated_head = current_repository_revision(root)
    except VcsError as error:
        return _message(f"Enoch could not update: {error}")

    if previous_head == updated_head:
        pending_promotions = promotions_pending_adoption(root, updated_head)
        if pending_promotions:
            doctor = run_update_doctor(root)
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

    doctor = run_update_doctor(root)
    if not doctor.passed:
        try:
            restore_repository_revision(previous_head, root)
            rollback = f"Rolled back to {previous_head[:7]}."
        except VcsError as error:
            rollback = f"Rollback failed: {error}"
        return _message(
            "\n\n".join(
                [
                    f"Enoch updated to latest {authoritative}, but doctor failed. I am not restarting.",
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
                f"Enoch updated to latest {authoritative} and doctor passed.",
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


def update_from_main(root: Path) -> UpdateResult:
    """Compatibility alias for integrations using the original Git-specific name."""
    return update_from_authoritative(root)


def _message(message: str) -> UpdateResult:
    return UpdateResult(message=message, direct_action_result="")


def run_update_doctor(root: Path) -> ImmuneResult:
    environment = os.environ.copy()
    source_root = str(root / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else source_root
    )
    python = environment.get("ENOCH_PYTHON") or sys.executable
    try:
        completed = subprocess.run(
            [python, "-m", "enoch.operations.update_doctor"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=UPDATE_DOCTOR_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _doctor_runner_failure(str(error))

    try:
        payload = json.loads(completed.stdout)
        return _doctor_result_from_payload(payload)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
        detail = completed.stderr.strip() or completed.stdout.strip() or str(error)
        return _doctor_runner_failure(detail)


def _doctor_result_from_payload(payload: object) -> ImmuneResult:
    if not isinstance(payload, dict):
        raise TypeError("Fresh doctor payload must be an object.")
    raw_diagnosis = payload["diagnosis"]
    raw_checks = payload["checks"]
    if not isinstance(raw_diagnosis, dict) or not isinstance(raw_checks, list):
        raise TypeError("Fresh doctor payload has invalid diagnosis or checks.")
    diagnosis = DoctorDiagnosis(
        summary=str(raw_diagnosis.get("summary") or ""),
        failing_tests=_string_list(raw_diagnosis.get("failing_tests")),
        likely_files=_string_list(raw_diagnosis.get("likely_files")),
        suggested_action=str(raw_diagnosis.get("suggested_action") or ""),
    )
    checks = [
        DoctorCheckResult(
            name=str(raw["name"]),
            passed=raw.get("passed") is True,
            command=str(raw.get("command") or ""),
            output=str(raw.get("output") or ""),
            category=str(raw.get("category") or "code health"),
            summary=str(raw.get("summary") or ""),
        )
        for raw in raw_checks
        if isinstance(raw, dict)
    ]
    return ImmuneResult(
        passed=payload.get("passed") is True,
        command=str(payload.get("command") or ""),
        output=str(payload.get("output") or ""),
        diagnosis=diagnosis,
        checks=checks,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _doctor_runner_failure(detail: str) -> ImmuneResult:
    check = DoctorCheckResult(
        name="fresh doctor process",
        passed=False,
        command=f"{sys.executable} -m enoch.operations.update_doctor",
        output=detail,
        category="operational readiness",
        summary="could not load updated health checks",
    )
    return ImmuneResult(
        passed=False,
        command=check.command,
        output=detail,
        diagnosis=DoctorDiagnosis(
            summary="Fresh post-update doctor process failed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="Inspect the fresh doctor process output before retrying the update.",
        ),
        checks=[check],
    )


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
    selected_channel = provider_name("chat", root)
    lifecycle = _load_channel_lifecycle_state(selected_channel, root)
    if str(lifecycle.get("status") or "") != "running":
        return ""
    if _int(lifecycle.get("pid")) != os.getpid():
        return ""
    started_head = str(lifecycle.get("started_head") or "").strip()
    if not started_head or started_head == current:
        return ""
    return "\n".join(
        [
            f"Local code is current at {current[:7]}, but this {provider_label(selected_channel)} daemon started on {started_head[:7]}.",
            "Run /restart to load the current code.",
        ]
    )


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _load_channel_lifecycle_state(name: str, root: Path) -> dict:
    return load_channel_lifecycle(name, root)
