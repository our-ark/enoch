from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from enoch.evolution.core import get_evolve_candidate, load_evolve_state
from enoch.evolution.events import (
    EvolveEvent,
    linked_proposal_id,
    load_evolve_events,
    record_evolve_event,
)
from enoch.vcs_tools import VcsError, revision_is_ancestor
from enoch.memory.paths import atomic_write
from enoch.paths import enoch_home
from enoch.state import StateCorruptionError, load_json_object
from enoch.providers.contracts import (
    ForgeProvider,
    ForgeProviderError,
    PullRequestMergeStatus,
)
from enoch.providers.registry import load_provider
from enoch.providers.forge import inspect_pull_request_merge
from enoch.tasks.events import TaskEvent, load_task_events
from enoch.operations.update_tools import (
    authoritative_branch_name,
    current_repository_revision,
    refresh_repository,
    revision_on_authoritative,
)


PENDING_ADOPTION_SCHEMA_VERSION = 1
RECORDING_MODES = {"realtime", "backfill"}


class EvolveLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvolutionReconcileResult:
    candidate_id: str
    pr_url: str
    merge_commit: str
    authoritative_branch: str
    promoted_at: str
    recording_mode: str
    event: EvolveEvent
    already_recorded: bool = False


@dataclass(frozen=True)
class PendingAdoption:
    candidate_id: str
    task_id: int | None
    pr_url: str
    merge_commit: str
    authoritative_branch: str
    promoted_at: str
    version: str
    health_check: str
    recording_mode: str


def pending_adoption_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "pending_evolve_adoptions.json"


def reconcile_evolve_candidate(
    candidate_id: str,
    root: Path,
    *,
    recording_mode: str = "realtime",
    forge: ForgeProvider | None = None,
) -> EvolutionReconcileResult:
    mode = _recording_mode(recording_mode)
    try:
        candidate = get_evolve_candidate(candidate_id, root)
    except ValueError as error:
        raise EvolveLifecycleError(str(error)) from error
    if candidate.status != "done":
        raise EvolveLifecycleError(
            f"Evolve candidate {candidate.id} must be done before promotion reconciliation."
        )
    task_event = _completed_task_with_pr(candidate.id, root)
    if task_event is None:
        raise EvolveLifecycleError(
            f"Evolve candidate {candidate.id} has no completed task with a pull request."
        )
    pr_url = task_event.pr_urls[-1]
    try:
        pull_request = (
            forge.inspect_pull_request_merge(pr_url, root)
            if forge is not None
            else inspect_pull_request_merge(pr_url, root)
        )
    except ForgeProviderError as error:
        raise EvolveLifecycleError(f"Could not inspect {pr_url}: {error}") from error
    authoritative = authoritative_branch_name(root)
    _validate_merged_pull_request(pull_request, authoritative)
    try:
        refresh_repository(root)
    except VcsError as error:
        raise EvolveLifecycleError(
            f"Could not refresh authoritative branch {authoritative}: {error}"
        ) from error
    if not revision_on_authoritative(pull_request.merge_commit, root):
        raise EvolveLifecycleError(
            f"PR merge revision {pull_request.merge_commit} is not on trusted "
            f"authoritative branch {authoritative}."
        )

    existing = _promoted_event(candidate.id, pull_request.merge_commit, root)
    if existing is not None:
        return _result_from_event(existing, already_recorded=True)

    state = load_evolve_state(root)
    event = record_evolve_event(
        "promoted",
        root,
        event_actor="human",
        trigger="/evolve reconcile",
        mode=state.mode,
        theme=state.theme,
        candidate=candidate,
        task_id=task_event.task_id,
        proposal_id=linked_proposal_id(
            root,
            candidate_id=candidate.id,
            task_id=task_event.task_id,
        ),
        pr_url=pull_request.url,
        merge_commit=pull_request.merge_commit,
        authoritative_branch=pull_request.base_branch,
        promoted_at=pull_request.merged_at,
        recording_mode=mode,
    )
    return _result_from_event(event)


def format_reconcile_result(result: EvolutionReconcileResult) -> str:
    action = "already recorded as promoted" if result.already_recorded else "recorded as promoted"
    return "\n".join(
        [
            f"Evolve candidate {result.candidate_id} {action}.",
            f"PR: {result.pr_url}",
            f"Merge commit: {result.merge_commit}",
            f"Authoritative branch: {result.authoritative_branch}",
            f"Promoted at: {result.promoted_at}",
            f"Recording mode: {result.recording_mode}",
            "Adoption remains pending until the instance updates and passes health checks.",
        ]
    )


def promotions_pending_adoption(root: Path, version: str) -> tuple[EvolveEvent, ...]:
    events = load_evolve_events(root)
    adopted = {
        (event.candidate_id, event.merge_commit)
        for event in events
        if event.event == "adopted" and event.merge_commit
    }
    pending: dict[tuple[str, str], EvolveEvent] = {}
    for event in events:
        key = (event.candidate_id, event.merge_commit)
        if (
            event.event == "promoted"
            and event.merge_commit
            and key not in adopted
            and _is_ancestor(event.merge_commit, version, root)
        ):
            pending[key] = event
    return tuple(pending.values())


def stage_promoted_evolve_adoptions(
    root: Path,
    version: str,
    *,
    health_check: str,
) -> tuple[PendingAdoption, ...]:
    if health_check.strip().lower() != "passed":
        return ()
    default_branch = authoritative_branch_name(root)
    pending = tuple(
        PendingAdoption(
            candidate_id=event.candidate_id,
            task_id=event.task_id,
            pr_url=event.pr_url,
            merge_commit=event.merge_commit,
            authoritative_branch=event.authoritative_branch or default_branch,
            promoted_at=event.promoted_at,
            version=version,
            health_check="passed",
            recording_mode=event.recording_mode or "realtime",
        )
        for event in promotions_pending_adoption(root, version)
    )
    if not pending:
        return ()
    atomic_write(
        pending_adoption_path(root),
        json.dumps(
            {
                "schema_version": PENDING_ADOPTION_SCHEMA_VERSION,
                "adoptions": [asdict(item) for item in pending],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return pending


def finalize_promoted_evolve_adoptions(
    root: Path,
    *,
    running_version: str = "",
) -> tuple[EvolveEvent, ...]:
    pending = _load_pending_adoptions(root)
    if not pending:
        return ()
    version = running_version.strip() or current_repository_revision(root)
    completed: list[EvolveEvent] = []
    remaining: list[PendingAdoption] = []
    for item in pending:
        if item.version != version or item.health_check != "passed":
            remaining.append(item)
            continue
        existing = _adopted_event(item.candidate_id, item.merge_commit, root)
        if existing is not None:
            continue
        try:
            candidate = get_evolve_candidate(item.candidate_id, root)
        except ValueError:
            remaining.append(item)
            continue
        state = load_evolve_state(root)
        completed.append(
            record_evolve_event(
                "adopted",
                root,
                event_actor="system",
                trigger="daemon-startup",
                mode=state.mode,
                theme=state.theme,
                candidate=candidate,
                task_id=item.task_id,
                proposal_id=linked_proposal_id(
                    root,
                    candidate_id=item.candidate_id,
                    task_id=item.task_id,
                ),
                pr_url=item.pr_url,
                merge_commit=item.merge_commit,
                authoritative_branch=item.authoritative_branch,
                promoted_at=item.promoted_at,
                version=item.version,
                health_check=item.health_check,
                recording_mode=item.recording_mode,
            )
        )
    _write_pending_adoptions(remaining, root)
    return tuple(completed)


def _completed_task_with_pr(
    candidate_id: str,
    root: Path,
) -> TaskEvent | None:
    matches = [
        event
        for event in load_task_events(root)
        if event.candidate_id == candidate_id
        and event.event == "completed"
        and event.pr_urls
    ]
    return max(matches, key=lambda event: (event.task_id, event.occurred_at), default=None)


def _validate_merged_pull_request(
    status: PullRequestMergeStatus,
    authoritative: str,
) -> None:
    if status.state != "MERGED":
        raise EvolveLifecycleError(f"Pull request {status.url} is not merged.")
    if status.base_branch != authoritative:
        raise EvolveLifecycleError(
            f"Pull request {status.url} targets {status.base_branch}, "
            f"not authoritative {authoritative}."
        )
    if not status.merge_commit or not status.merged_at:
        raise EvolveLifecycleError(
            f"Pull request {status.url} is missing merge commit evidence."
        )


def _promoted_event(
    candidate_id: str,
    merge_commit: str,
    root: Path,
) -> EvolveEvent | None:
    return next(
        (
            event
            for event in reversed(load_evolve_events(root, candidate_id=candidate_id))
            if event.event == "promoted" and event.merge_commit == merge_commit
        ),
        None,
    )


def _adopted_event(
    candidate_id: str,
    merge_commit: str,
    root: Path,
) -> EvolveEvent | None:
    return next(
        (
            event
            for event in reversed(load_evolve_events(root, candidate_id=candidate_id))
            if event.event == "adopted" and event.merge_commit == merge_commit
        ),
        None,
    )


def _result_from_event(
    event: EvolveEvent,
    *,
    already_recorded: bool = False,
) -> EvolutionReconcileResult:
    return EvolutionReconcileResult(
        candidate_id=event.candidate_id,
        pr_url=event.pr_url,
        merge_commit=event.merge_commit,
        authoritative_branch=event.authoritative_branch,
        promoted_at=event.promoted_at,
        recording_mode=event.recording_mode,
        event=event,
        already_recorded=already_recorded,
    )


def _recording_mode(value: str) -> str:
    normalized = value.strip().lower() or "realtime"
    if normalized not in RECORDING_MODES:
        raise EvolveLifecycleError("Recording mode must be realtime or backfill.")
    return normalized


def _is_ancestor(revision: str, descendant: str, root: Path) -> bool:
    return revision_is_ancestor(revision, descendant, root)


def _load_pending_adoptions(root: Path) -> tuple[PendingAdoption, ...]:
    path = pending_adoption_path(root)
    data = load_json_object(path)
    if not data:
        return ()
    raw_items = data.get("adoptions")
    if not isinstance(raw_items, list):
        raise StateCorruptionError(path, "expected adoptions to be a list")
    items = []
    default_branch = authoritative_branch_name(root)
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("candidate_id") or "").strip()
        merge_commit = str(raw.get("merge_commit") or "").strip()
        version = str(raw.get("version") or "").strip()
        if not candidate_id or not merge_commit or not version:
            continue
        items.append(
            PendingAdoption(
                candidate_id=candidate_id,
                task_id=_positive_int(raw.get("task_id")),
                pr_url=str(raw.get("pr_url") or "").strip(),
                merge_commit=merge_commit,
                authoritative_branch=str(
                    raw.get("authoritative_branch") or default_branch
                ).strip(),
                promoted_at=str(raw.get("promoted_at") or "").strip(),
                version=version,
                health_check=str(raw.get("health_check") or "").strip().lower(),
                recording_mode=_recording_mode(
                    str(raw.get("recording_mode") or "realtime")
                ),
            )
        )
    return tuple(items)


def _write_pending_adoptions(
    pending: list[PendingAdoption],
    root: Path,
) -> None:
    path = pending_adoption_path(root)
    if not pending:
        path.unlink(missing_ok=True)
        return
    atomic_write(
        path,
        json.dumps(
            {
                "schema_version": PENDING_ADOPTION_SCHEMA_VERSION,
                "adoptions": [asdict(item) for item in pending],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
