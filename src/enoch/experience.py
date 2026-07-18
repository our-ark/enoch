from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import TYPE_CHECKING

from enoch.memory.paths import clean_text
from enoch.paths import enoch_home
from enoch.task_events import TaskEvent, load_task_events, record_task_event

if TYPE_CHECKING:
    from enoch.task_queue import TaskJob


TERMINAL_OUTCOMES = {
    "completed",
    "failed",
    "cancelled",
    "regressed",
    "reverted",
    "forward-fixed",
}


@dataclass(frozen=True)
class ExperienceRecord:
    id: str
    task_id: int
    created_at: str
    updated_at: str
    command: str
    outcome: str
    request: str
    result_summary: str
    context_source: str
    source: str
    initiated_by: str
    candidate_id: str
    parent_task_id: int | None
    pr_urls: tuple[str, ...]
    changed_files: tuple[str, ...]
    started: bool
    regressed: bool = False
    regression_resolution: str = ""
    regression_related_task_id: int | None = None
    evidence_source: str = ""
    signal_actor: str = ""
    candidate_actor: str = ""
    approval_actor: str = ""
    parent_candidate_id: str = ""
    source_task_id: int | None = None


def experience_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "experience.jsonl"


def record_task_experience(
    job: TaskJob,
    root: Path | None = None,
    *,
    command: str = "",
    result: str = "",
) -> ExperienceRecord:
    outcome = clean_text(job.status).lower()
    if outcome not in TERMINAL_OUTCOMES:
        raise ValueError(
            "Experience can only record completed, failed, cancelled, regressed, reverted, "
            "or forward-fixed tasks."
        )
    existing = next((record for record in load_experience_records(root) if record.task_id == job.id), None)
    if existing is not None and existing.outcome == outcome:
        return existing
    if not load_task_events(root, task_id=job.id):
        record_task_event(
            job,
            "created",
            root,
            event_actor=getattr(job, "initiated_by", "human"),
            trigger=command,
        )
        if job.started_at:
            record_task_event(
                job,
                "started",
                root,
                event_actor="system",
                trigger="task-runner",
            )
    record_task_event(
        job,
        outcome,
        root,
        event_actor="agent" if outcome in {"completed", "failed"} else "human",
        trigger=command,
        result=result,
    )
    return next(record for record in load_experience_records(root) if record.task_id == job.id)


def load_experience_records(
    root: Path | None = None,
    *,
    limit: int = 200,
) -> tuple[ExperienceRecord, ...]:
    if limit <= 0:
        return ()
    event_limit = max(5000, limit * 8)
    records = {
        record.task_id: record
        for record in _records_from_events(load_task_events(root, limit=event_limit))
    }
    for legacy in _load_legacy_records(root):
        records.setdefault(legacy.task_id, legacy)
    return tuple(
        sorted(records.values(), key=lambda item: (item.updated_at, item.task_id), reverse=True)[:limit]
    )


def _records_from_events(events: tuple[TaskEvent, ...]) -> tuple[ExperienceRecord, ...]:
    grouped: dict[int, list[TaskEvent]] = {}
    for event in events:
        grouped.setdefault(event.task_id, []).append(event)
    records = []
    for task_events in grouped.values():
        first = task_events[0]
        latest = task_events[-1]
        result_event = next((event for event in reversed(task_events) if event.result_summary), latest)
        regression = next(
            (event for event in reversed(task_events) if event.event == "regressed"),
            None,
        )
        resolution = None
        if regression is not None:
            resolution = next(
                (
                    event
                    for event in reversed(task_events)
                    if event.event in {"reverted", "forward-fixed"}
                    and event.occurred_at >= regression.occurred_at
                ),
                None,
            )
        records.append(
            ExperienceRecord(
                id=f"task-{first.task_id}",
                task_id=first.task_id,
                created_at=first.occurred_at,
                updated_at=latest.occurred_at,
                command=first.trigger,
                outcome=latest.event,
                request=first.request,
                result_summary=result_event.result_summary,
                context_source=latest.context_source or first.context_source,
                source=first.source,
                initiated_by=first.initiated_by,
                candidate_id=first.candidate_id,
                parent_task_id=first.parent_task_id,
                pr_urls=_merge_tuples(event.pr_urls for event in task_events),
                changed_files=_merge_tuples(event.changed_files for event in task_events),
                started=any(event.event == "started" for event in task_events),
                regressed=regression is not None,
                regression_resolution=resolution.event if resolution is not None else "",
                regression_related_task_id=resolution.related_task_id if resolution is not None else None,
                evidence_source=first.evidence_source,
                signal_actor=first.signal_actor,
                candidate_actor=first.candidate_actor,
                approval_actor=first.approval_actor,
                parent_candidate_id=first.parent_candidate_id,
                source_task_id=first.source_task_id,
            )
        )
    return tuple(records)


def _load_legacy_records(root: Path | None) -> tuple[ExperienceRecord, ...]:
    path = experience_path(root)
    if not path.exists():
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    records: list[ExperienceRecord] = []
    seen: set[int] = set()
    for line in reversed(lines):
        record = _legacy_record_from_line(line)
        if record is None or record.task_id in seen:
            continue
        seen.add(record.task_id)
        records.append(record)
    return tuple(records)


def _legacy_record_from_line(line: str) -> ExperienceRecord | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    task_id = _positive_int(raw.get("task_id"))
    outcome = clean_text(str(raw.get("outcome") or "")).lower()
    request = clean_text(str(raw.get("request") or ""))
    if task_id is None or outcome not in TERMINAL_OUTCOMES or not request:
        return None
    created_at = str(raw.get("created_at") or "")
    return ExperienceRecord(
        id=clean_text(str(raw.get("id") or "")) or f"task-{task_id}",
        task_id=task_id,
        created_at=created_at,
        updated_at=created_at,
        command=str(raw.get("command") or "").strip(),
        outcome=outcome,
        request=request,
        result_summary=str(raw.get("result_summary") or "").strip(),
        context_source=str(raw.get("context_source") or "").strip(),
        source="task",
        initiated_by="human",
        candidate_id="",
        parent_task_id=None,
        pr_urls=_string_tuple(raw.get("pr_urls")),
        changed_files=_string_tuple(raw.get("changed_files")),
        started=bool(raw.get("started", False)),
        regressed=bool(raw.get("regressed", False)),
        regression_resolution=clean_text(str(raw.get("regression_resolution") or "")).lower(),
        regression_related_task_id=_positive_int(raw.get("regression_related_task_id")),
        evidence_source="",
        signal_actor="",
        candidate_actor="",
        approval_actor="",
        parent_candidate_id="",
        source_task_id=None,
    )


def _merge_tuples(groups) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                output.append(item)
    return tuple(output)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return _merge_tuples([[str(item).strip() for item in value]])


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
