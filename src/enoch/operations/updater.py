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
from enoch.immune import DoctorCheckResult, DoctorDiagnosis, ImmuneResult
from enoch.providers import as_repository_provider
from enoch.providers.contracts import (
    RepositoryProvider,
    RepositoryProviderError,
)
from enoch.providers.registry import provider_name
from enoch.providers.registry import load_provider


@dataclass(frozen=True)
class UpdateResult:
    message: str
    direct_action_result: str
    restart_required: bool = False
    previous_revision_id: str = ""
    revision_id: str = ""
    authoritative_name: str = ""


UPDATE_DOCTOR_TIMEOUT_SECONDS = 300


def update_from_authoritative(
    root: Path,
    *,
    repository: RepositoryProvider | None = None,
) -> UpdateResult:
    repository = repository or as_repository_provider(load_provider("vcs", root))
    try:
        working_copy = repository.inspect_working_copy(root)
        if not working_copy.clean:
            changed = ", ".join(working_copy.changed_paths[:8])
            detail = f": {changed}" if changed else ""
            raise RepositoryProviderError(
                f"working copy has uncommitted changes{detail}"
            )
        authoritative = repository.authoritative_base(root, refresh=True)
        previous_revision = working_copy.revision
        if (
            previous_revision.id != authoritative.revision.id
            and not repository.repository_is_ancestor(
                previous_revision,
                authoritative.revision,
                root,
            )
        ):
            return _message(
                "Enoch could not update: current revision "
                f"{previous_revision.id} is not in the history of authoritative "
                f"revision {authoritative.revision.id}. Finish or publish that work first."
            )
        if previous_revision.id != authoritative.revision.id:
            repository.restore_repository_revision(
                authoritative.revision,
                root,
            )
        updated_revision = repository.inspect_working_copy(root).revision
        if updated_revision.id != authoritative.revision.id:
            raise RepositoryProviderError(
                "Repository provider did not activate authoritative revision "
                f"{authoritative.revision.id}."
            )
    except RepositoryProviderError as error:
        return _message(f"Enoch could not update: {error}")

    previous_head = previous_revision.id
    updated_head = updated_revision.id
    authoritative_name = authoritative.name or "authoritative repository"
    update_summary = (
        "Already at authoritative revision "
        f"{updated_head}."
        if previous_head == updated_head
        else (
            f"Updated repository from {previous_head} to {updated_head} "
            f"using {authoritative_name}."
        )
    )
    if previous_head == updated_head:
        pending_promotions = promotions_pending_adoption(
            root,
            updated_head,
            repository=repository,
        )
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
            staged_note = _stage_adoptions(
                root,
                updated_head,
                repository=repository,
            )
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
                        update_summary,
                        formatted_doctor,
                        staged_note,
                        f"Restarting into {updated_head[:7]}.",
                    ]
                    if part
                ),
                restart_required=True,
                previous_revision_id=previous_head,
                revision_id=updated_head,
                authoritative_name=authoritative.name,
            )
        restart_note = _running_commit_restart_note(root, updated_head)
        return UpdateResult(
            message="\n\n".join(part for part in ["Enoch is already up to date.", restart_note] if part),
            direct_action_result="\n\n".join(
                part for part in [update_summary, restart_note] if part
            ),
            previous_revision_id=previous_head,
            revision_id=updated_head,
            authoritative_name=authoritative.name,
        )

    doctor = run_update_doctor(root)
    if not doctor.passed:
        try:
            repository.restore_repository_revision(previous_revision, root)
            rollback = f"Rolled back to {previous_head[:7]}."
        except RepositoryProviderError as error:
            rollback = f"Rollback failed: {error}"
        return _message(
            "\n\n".join(
                [
                    f"Enoch updated to latest {authoritative_name}, but doctor failed. I am not restarting.",
                    format_doctor_result(doctor),
                    rollback,
                    "The currently running Enoch process is still the pre-update code.",
                ]
            )
        )

    formatted_doctor = format_doctor_result(doctor)
    staged_note = _stage_adoptions(
        root,
        updated_head,
        repository=repository,
    )
    return UpdateResult(
        message="\n\n".join(
            part
            for part in [
                f"Enoch updated to latest {authoritative_name} and doctor passed.",
                formatted_doctor,
                staged_note,
                "Restarting now. The startup notification will confirm Enoch came back.",
            ]
            if part
        ),
        direct_action_result="\n\n".join(
            part
            for part in [
                update_summary,
                formatted_doctor,
                staged_note,
                f"Restarting into {updated_head[:7]}.",
            ]
            if part
        ),
        restart_required=True,
        previous_revision_id=previous_head,
        revision_id=updated_head,
        authoritative_name=authoritative.name,
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
            skipped=raw.get("skipped") is True,
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


def _stage_adoptions(
    root: Path,
    version: str,
    *,
    repository: RepositoryProvider,
) -> str:
    try:
        staged = stage_promoted_evolve_adoptions(
            root,
            version,
            health_check="passed",
            repository=repository,
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
