from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from enoch.memory.paths import now as current_time
from enoch.paths import private_state_path
from enoch.providers.contracts import ConversationId, normalize_conversation_id
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object


SCHEMA_VERSION = 1
QUEUE_PATH = Path("lineage") / "assessment_queue.json"
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
JOB_STATUSES = {PENDING, RUNNING, COMPLETED, FAILED}


@dataclass(frozen=True)
class LineageAssessmentJob:
    id: str
    conversation_id: ConversationId
    candidate_ids: tuple[str, ...]
    requested_at: str
    status: str = PENDING
    new_count: int = 0
    started_at: str = ""
    completed_at: str = ""
    owner_epoch: str = ""
    attempts: int = 0
    assessed_count: int = 0
    failed_count: int = 0
    error: str = ""

    @property
    def total_count(self) -> int:
        return len(self.candidate_ids)


@dataclass(frozen=True)
class LineageAssessmentQueue:
    current: LineageAssessmentJob | None = None
    last: LineageAssessmentJob | None = None


def lineage_assessment_queue_file(root: Path | None = None) -> Path:
    return private_state_path(QUEUE_PATH, root)


def load_lineage_assessment_queue(
    root: Path | None = None,
) -> LineageAssessmentQueue:
    path = lineage_assessment_queue_file(root)
    with file_transaction(path):
        return _load_queue(path)


def enqueue_lineage_assessment(
    conversation_id: ConversationId,
    candidate_ids: tuple[str, ...],
    root: Path | None = None,
    *,
    new_count: int = 0,
) -> tuple[LineageAssessmentJob, bool]:
    normalized_ids = tuple(
        dict.fromkeys(
            candidate_id.strip()
            for candidate_id in candidate_ids
            if candidate_id.strip()
        )
    )
    if not normalized_ids:
        raise ValueError("An inheritance assessment requires at least one change.")
    normalized_conversation_id = normalize_conversation_id(conversation_id)
    if normalized_conversation_id is None:
        raise ValueError("An inheritance assessment requires a conversation id.")
    path = lineage_assessment_queue_file(root)
    with file_transaction(path):
        queue = _load_queue(path)
        if queue.current is not None:
            return queue.current, False
        job = LineageAssessmentJob(
            id=f"lineage-{uuid4().hex}",
            conversation_id=normalized_conversation_id,
            candidate_ids=normalized_ids,
            requested_at=current_time(),
            new_count=max(0, int(new_count)),
        )
        _write_queue(path, LineageAssessmentQueue(current=job, last=queue.last))
        return job, True


def claim_lineage_assessment(
    owner_epoch: str,
    root: Path | None = None,
) -> LineageAssessmentJob | None:
    owner = owner_epoch.strip()
    if not owner:
        raise ValueError("A daemon epoch is required to claim inheritance assessment.")
    path = lineage_assessment_queue_file(root)
    with file_transaction(path):
        queue = _load_queue(path)
        job = queue.current
        if job is None:
            return None
        if job.status == RUNNING and job.owner_epoch == owner:
            return None
        claimed = replace(
            job,
            status=RUNNING,
            started_at=current_time(),
            completed_at="",
            owner_epoch=owner,
            attempts=job.attempts + 1,
            error="",
        )
        _write_queue(path, replace(queue, current=claimed))
        return claimed


def complete_lineage_assessment(
    job_id: str,
    root: Path | None = None,
    *,
    owner_epoch: str,
    assessed_count: int,
    failed_count: int,
) -> LineageAssessmentJob | None:
    return _finish_lineage_assessment(
        job_id,
        COMPLETED,
        root,
        owner_epoch=owner_epoch,
        assessed_count=assessed_count,
        failed_count=failed_count,
    )


def fail_lineage_assessment(
    job_id: str,
    error: str,
    root: Path | None = None,
    *,
    owner_epoch: str,
) -> LineageAssessmentJob | None:
    return _finish_lineage_assessment(
        job_id,
        FAILED,
        root,
        owner_epoch=owner_epoch,
        error=" ".join(error.split())[:1000],
    )


def _finish_lineage_assessment(
    job_id: str,
    status: str,
    root: Path | None,
    *,
    owner_epoch: str,
    assessed_count: int = 0,
    failed_count: int = 0,
    error: str = "",
) -> LineageAssessmentJob | None:
    if status not in {COMPLETED, FAILED}:
        raise ValueError(f"Invalid inheritance assessment completion status: {status}")
    owner = owner_epoch.strip()
    if not owner:
        raise ValueError("A daemon epoch is required to finish inheritance assessment.")
    path = lineage_assessment_queue_file(root)
    with file_transaction(path):
        queue = _load_queue(path)
        job = queue.current
        if (
            job is None
            or job.id != job_id.strip()
            or job.owner_epoch != owner
        ):
            return None
        finished = replace(
            job,
            status=status,
            completed_at=current_time(),
            owner_epoch="",
            assessed_count=max(0, int(assessed_count)),
            failed_count=max(0, int(failed_count)),
            error=error,
        )
        _write_queue(
            path,
            LineageAssessmentQueue(current=None, last=finished),
        )
        return finished


def _load_queue(path: Path) -> LineageAssessmentQueue:
    raw = load_json_object(
        path,
        default_factory=lambda: {
            "schema_version": SCHEMA_VERSION,
            "current": None,
            "last": None,
        },
    )
    schema_version = _int(raw.get("schema_version"))
    if schema_version != SCHEMA_VERSION:
        raise StateCorruptionError(
            path,
            f"unsupported schema version {schema_version}",
        )
    current = _job_from_json(raw.get("current"))
    last = _job_from_json(raw.get("last"))
    if raw.get("current") is not None and current is None:
        raise StateCorruptionError(path, "found an invalid current assessment job")
    if raw.get("last") is not None and last is None:
        raise StateCorruptionError(path, "found an invalid last assessment job")
    if current is not None and current.status not in {PENDING, RUNNING}:
        raise StateCorruptionError(path, "current assessment job is not pending or running")
    if last is not None and last.status not in {COMPLETED, FAILED}:
        raise StateCorruptionError(path, "last assessment job is not completed or failed")
    return LineageAssessmentQueue(current=current, last=last)


def _write_queue(path: Path, queue: LineageAssessmentQueue) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "current": _job_to_json(queue.current),
        "last": _job_to_json(queue.last),
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _job_to_json(job: LineageAssessmentJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    payload = asdict(job)
    payload["candidate_ids"] = list(job.candidate_ids)
    return payload


def _job_from_json(raw: object) -> LineageAssessmentJob | None:
    if not isinstance(raw, dict):
        return None
    job_id = str(raw.get("id") or "").strip()
    requested_at = str(raw.get("requested_at") or "").strip()
    status = str(raw.get("status") or "").strip()
    raw_candidate_ids = raw.get("candidate_ids")
    if not isinstance(raw_candidate_ids, list):
        return None
    candidate_ids = tuple(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw_candidate_ids
            if str(item or "").strip()
        )
    )
    conversation_id = normalize_conversation_id(raw.get("conversation_id"))
    if (
        not job_id
        or not requested_at
        or conversation_id is None
        or status not in JOB_STATUSES
        or not candidate_ids
    ):
        return None
    return LineageAssessmentJob(
        id=job_id,
        conversation_id=conversation_id,
        candidate_ids=candidate_ids,
        requested_at=requested_at,
        status=status,
        new_count=max(0, _int(raw.get("new_count"))),
        started_at=str(raw.get("started_at") or ""),
        completed_at=str(raw.get("completed_at") or ""),
        owner_epoch=str(raw.get("owner_epoch") or ""),
        attempts=max(0, _int(raw.get("attempts"))),
        assessed_count=max(0, _int(raw.get("assessed_count"))),
        failed_count=max(0, _int(raw.get("failed_count"))),
        error=str(raw.get("error") or ""),
    )


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
