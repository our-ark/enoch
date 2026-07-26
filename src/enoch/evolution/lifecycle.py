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
from enoch.memory.paths import atomic_write
from enoch.paths import private_state_path
from enoch.state import StateCorruptionError, load_json_object
from enoch.providers.contracts import (
    RepositoryProvider,
    RepositoryProviderError,
    RepositoryRevision,
    ReviewIdentity,
    ReviewProvider,
    ReviewProviderError,
    ReviewRecord,
)
from enoch.providers import as_repository_provider, as_review_provider
from enoch.providers.registry import load_provider
from enoch.tasks.events import TaskEvent, load_task_events


PENDING_ADOPTION_SCHEMA_VERSION = 2
RECORDING_MODES = {"realtime", "backfill"}


class EvolveLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvolutionReconcileResult:
    candidate_id: str
    review_id: str
    review_urls: tuple[str, ...]
    revision_id: str
    authoritative_revision_id: str
    authoritative_name: str
    promoted_at: str
    recording_mode: str
    event: EvolveEvent
    already_recorded: bool = False

    @property
    def pr_url(self) -> str:
        return self.review_urls[-1] if self.review_urls else ""

    @property
    def merge_commit(self) -> str:
        return self.revision_id

    @property
    def authoritative_branch(self) -> str:
        return self.authoritative_name


@dataclass(frozen=True)
class PendingAdoption:
    candidate_id: str
    task_id: int | None
    review_id: str
    review_urls: tuple[str, ...]
    revision_id: str
    authoritative_revision_id: str
    authoritative_name: str
    promoted_at: str
    version: str
    health_check: str
    recording_mode: str


def pending_adoption_path(root: Path | None = None) -> Path:
    return private_state_path("pending_evolve_adoptions.json", root)


def reconcile_evolve_candidate(
    candidate_id: str,
    root: Path,
    *,
    recording_mode: str = "realtime",
    repository: RepositoryProvider | None = None,
    review: ReviewProvider | None = None,
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
    task_event = _completed_task_with_review(candidate.id, root)
    if task_event is None:
        raise EvolveLifecycleError(
            f"Evolve candidate {candidate.id} has no completed task with a published review."
        )
    repository = repository or as_repository_provider(load_provider("vcs", root))
    review = review or as_review_provider(load_provider("forge", root))
    review_url = task_event.review_urls[-1] if task_event.review_urls else ""
    review_identity = ReviewIdentity(
        id=task_event.review_id or review_url,
        url=review_url,
        metadata={"revision_id": task_event.revision_id},
    )
    try:
        review_record = review.inspect_review(review_identity, root)
        _validate_landed_review(review_record)
        authoritative = repository.authoritative_base(root, refresh=True)
    except (ReviewProviderError, RepositoryProviderError) as error:
        raise EvolveLifecycleError(
            f"Could not verify review {review_identity.id}: {error}"
        ) from error
    assert review_record.landed_revision is not None
    if not repository.repository_is_ancestor(
        review_record.landed_revision,
        authoritative.revision,
        root,
    ):
        raise EvolveLifecycleError(
            f"Landed revision {review_record.landed_revision.id} is not on trusted "
            f"authoritative revision {authoritative.revision.id}."
        )

    existing = _promoted_event(
        candidate.id,
        review_record.landed_revision.id,
        root,
    )
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
        review_id=review_record.identity.id,
        review_urls=tuple(
            url
            for url in (review_record.identity.url, *task_event.review_urls)
            if url
        ),
        revision_id=review_record.landed_revision.id,
        authoritative_revision_id=authoritative.revision.id,
        authoritative_name=authoritative.name,
        promoted_at=review_record.landed_at,
        recording_mode=mode,
    )
    return _result_from_event(event)


def format_reconcile_result(result: EvolutionReconcileResult) -> str:
    action = "already recorded as promoted" if result.already_recorded else "recorded as promoted"
    return "\n".join(
        [
            f"Evolve candidate {result.candidate_id} {action}.",
            f"Review: {result.review_id}",
            f"Landed revision: {result.revision_id}",
            f"Authoritative revision: {result.authoritative_revision_id}",
            f"Authoritative target: {result.authoritative_name or 'unnamed'}",
            f"Promoted at: {result.promoted_at}",
            f"Recording mode: {result.recording_mode}",
            "Adoption remains pending until the instance updates and passes health checks.",
        ]
    )


def promotions_pending_adoption(
    root: Path,
    version: str,
    *,
    repository: RepositoryProvider | None = None,
) -> tuple[EvolveEvent, ...]:
    repository = repository or as_repository_provider(load_provider("vcs", root))
    events = load_evolve_events(root)
    adopted = {
        (event.candidate_id, event.revision_id)
        for event in events
        if event.event == "adopted" and event.revision_id
    }
    pending: dict[tuple[str, str], EvolveEvent] = {}
    for event in events:
        key = (event.candidate_id, event.revision_id)
        if (
            event.event == "promoted"
            and event.revision_id
            and key not in adopted
            and repository.repository_is_ancestor(
                RepositoryRevision(event.revision_id),
                RepositoryRevision(version),
                root,
            )
        ):
            pending[key] = event
    return tuple(pending.values())


def stage_promoted_evolve_adoptions(
    root: Path,
    version: str,
    *,
    health_check: str,
    repository: RepositoryProvider | None = None,
) -> tuple[PendingAdoption, ...]:
    if health_check.strip().lower() != "passed":
        return ()
    pending = tuple(
        PendingAdoption(
            candidate_id=event.candidate_id,
            task_id=event.task_id,
            review_id=event.review_id,
            review_urls=event.review_urls,
            revision_id=event.revision_id,
            authoritative_revision_id=event.authoritative_revision_id,
            authoritative_name=event.authoritative_name,
            promoted_at=event.promoted_at,
            version=version,
            health_check="passed",
            recording_mode=event.recording_mode or "realtime",
        )
        for event in promotions_pending_adoption(
            root,
            version,
            repository=repository,
        )
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
    repository: RepositoryProvider | None = None,
) -> tuple[EvolveEvent, ...]:
    pending = _load_pending_adoptions(root)
    if not pending:
        return ()
    repository = repository or as_repository_provider(load_provider("vcs", root))
    version = (
        running_version.strip()
        or repository.inspect_working_copy(root).revision.id
    )
    completed: list[EvolveEvent] = []
    remaining: list[PendingAdoption] = []
    for item in pending:
        if item.version != version or item.health_check != "passed":
            remaining.append(item)
            continue
        existing = _adopted_event(item.candidate_id, item.revision_id, root)
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
                review_id=item.review_id,
                review_urls=item.review_urls,
                revision_id=item.revision_id,
                authoritative_revision_id=item.authoritative_revision_id,
                authoritative_name=item.authoritative_name,
                promoted_at=item.promoted_at,
                version=item.version,
                health_check=item.health_check,
                recording_mode=item.recording_mode,
            )
        )
    _write_pending_adoptions(remaining, root)
    return tuple(completed)


def _completed_task_with_review(
    candidate_id: str,
    root: Path,
) -> TaskEvent | None:
    matches = [
        event
        for event in load_task_events(root)
        if event.candidate_id == candidate_id
        and event.event == "completed"
        and (event.review_id or event.review_urls)
    ]
    return max(matches, key=lambda event: (event.task_id, event.occurred_at), default=None)


def _validate_landed_review(record: ReviewRecord) -> None:
    if record.state != "landed":
        raise EvolveLifecycleError(
            f"Review {record.identity.id} is not landed."
        )
    if record.landed_revision is None or not record.landed_at:
        raise EvolveLifecycleError(
            f"Review {record.identity.id} is missing landed revision evidence."
        )


def _promoted_event(
    candidate_id: str,
    revision_id: str,
    root: Path,
) -> EvolveEvent | None:
    return next(
        (
            event
            for event in reversed(load_evolve_events(root, candidate_id=candidate_id))
            if event.event == "promoted" and event.revision_id == revision_id
        ),
        None,
    )


def _adopted_event(
    candidate_id: str,
    revision_id: str,
    root: Path,
) -> EvolveEvent | None:
    return next(
        (
            event
            for event in reversed(load_evolve_events(root, candidate_id=candidate_id))
            if event.event == "adopted" and event.revision_id == revision_id
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
        review_id=event.review_id,
        review_urls=event.review_urls,
        revision_id=event.revision_id,
        authoritative_revision_id=event.authoritative_revision_id,
        authoritative_name=event.authoritative_name,
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


def _load_pending_adoptions(root: Path) -> tuple[PendingAdoption, ...]:
    path = pending_adoption_path(root)
    data = load_json_object(path)
    if not data:
        return ()
    raw_items = data.get("adoptions")
    if not isinstance(raw_items, list):
        raise StateCorruptionError(path, "expected adoptions to be a list")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("candidate_id") or "").strip()
        revision_id = str(
            raw.get("revision_id") or raw.get("merge_commit") or ""
        ).strip()
        version = str(raw.get("version") or "").strip()
        if not candidate_id or not revision_id or not version:
            continue
        review_url = str(raw.get("pr_url") or "").strip()
        raw_review_urls = raw.get("review_urls")
        review_urls = (
            tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in raw_review_urls
                    if str(item).strip()
                )
            )
            if isinstance(raw_review_urls, list)
            else ()
        )
        if review_url and review_url not in review_urls:
            review_urls = (*review_urls, review_url)
        items.append(
            PendingAdoption(
                candidate_id=candidate_id,
                task_id=_positive_int(raw.get("task_id")),
                review_id=str(
                    raw.get("review_id") or review_url
                ).strip(),
                review_urls=review_urls,
                revision_id=revision_id,
                authoritative_revision_id=str(
                    raw.get("authoritative_revision_id") or ""
                ).strip(),
                authoritative_name=str(
                    raw.get("authoritative_name")
                    or raw.get("authoritative_branch")
                    or ""
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
