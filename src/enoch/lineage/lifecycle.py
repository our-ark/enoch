from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from enoch.lineage.core import (
    LineageError,
    STATUS_LINKED,
    adopt_inbox_candidate,
    load_inbox_candidates,
)
from enoch.providers.contracts import (
    ReviewIdentity,
    ReviewProvider,
    ReviewProviderError,
)
from enoch.tasks.queue import TaskJob


LINEAGE_CONTEXT_PREFIX = "lineage:"


@dataclass(frozen=True)
class LineageReconcileResult:
    adopted_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def lineage_context_source(change_id: str) -> str:
    return f"{LINEAGE_CONTEXT_PREFIX}{change_id.strip()}"


def reconcile_lineage_adoptions(
    root: Path,
    tasks: Iterable[TaskJob],
    *,
    review: ReviewProvider,
) -> LineageReconcileResult:
    jobs = tuple(tasks)
    adopted: list[str] = []
    errors: list[str] = []
    linked = (
        candidate
        for candidate in load_inbox_candidates(root, include_inactive=True)
        if candidate.status == STATUS_LINKED
    )
    for candidate in linked:
        matches = tuple(
            job
            for job in jobs
            if job.context_source == lineage_context_source(candidate.id)
            or job.id == candidate.linked_task_id
        )
        completed = sorted(
            (
                job
                for job in matches
                if job.status == "completed"
                and (job.review_id or job.review_url or job.review_urls)
            ),
            key=lambda job: job.id,
            reverse=True,
        )
        landed = False
        for job in completed:
            identities = _review_identities(job)
            for identity in identities:
                try:
                    record = review.inspect_review(identity, root)
                except (ReviewProviderError, PermissionError, OSError, ValueError) as error:
                    errors.append(f"{candidate.id}: {error}")
                    continue
                if record.state != "landed" or record.landed_revision is None:
                    continue
                try:
                    adopt_inbox_candidate(
                        candidate.id,
                        record.landed_revision.id,
                        root,
                        note=(
                            f"Verified landed review "
                            f"{record.identity.url or record.identity.id}."
                        ),
                    )
                except LineageError as error:
                    errors.append(f"{candidate.id}: {error}")
                    continue
                adopted.append(candidate.id)
                landed = True
                break
            if landed:
                break
    return LineageReconcileResult(
        adopted_ids=tuple(adopted),
        errors=tuple(dict.fromkeys(errors)),
    )


def _review_identities(job: TaskJob) -> tuple[ReviewIdentity, ...]:
    values = [
        (job.review_id, job.review_url),
        *((url, url) for url in job.review_urls),
    ]
    identities: list[ReviewIdentity] = []
    seen: set[str] = set()
    for identifier, url in values:
        key = str(identifier or url or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        identities.append(
            ReviewIdentity(
                id=key,
                url=str(url or "").strip(),
            )
        )
    return tuple(identities)
