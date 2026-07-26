from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from enoch.evolution.curation import candidate_scope_is_safe
from enoch.logs import conversation_log_dirs
from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import artifact_path, artifact_read_paths, private_state_path
from enoch.state import atomic_write, file_transaction, load_json_object
from enoch.tasks.events import TaskEvent, load_task_events


SCHEMA_VERSION = 1
DEFAULT_FEEDBACK_BATCH_SIZE = 20
DEFAULT_EXPERIENCE_BATCH_SIZE = 20
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 100
MAX_SIGNALS_PER_SCAN = 10
MAX_CANDIDATES_PER_SYNTHESIS = 5
EVIDENCE_SOURCES = {"feedback", "experience"}
EVIDENCE_STATUSES = {"active", "linked", "dismissed", "resolved", "superseded"}
EvidenceGenerator = Callable[[str], str]

_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|bot[_ -]?token|token|password|secret|"
    r"authorization)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b[1-9]\d{5,}:[A-Za-z0-9_-]{20,}\b")
_SETUP_TOKEN_PATTERN = re.compile(
    r"(?i)(\b(?:bin/enoch\s+)?setup\s+token\s+)[^\s<>]+"
)
_KNOWN_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:gh[opsu]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
)
class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceSettings:
    feedback_batch_size: int = DEFAULT_FEEDBACK_BATCH_SIZE
    experience_batch_size: int = DEFAULT_EXPERIENCE_BATCH_SIZE


@dataclass(frozen=True)
class EvidenceSignal:
    id: str
    source: str
    observation: str
    evidence_type: str
    affected_area: str
    desired_outcome: str
    confidence: float
    explicit: bool
    evidence_refs: tuple[str, ...]
    created_at: str
    updated_at: str
    status: str = "active"
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceScanRecord:
    id: str
    source: str
    created_at: str
    status: str
    reason: str
    input_refs: tuple[str, ...]
    task_markers: dict[int, str]
    evidence_ids: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class EvidenceScanResult:
    source: str
    processed: int
    evidence: tuple[EvidenceSignal, ...]
    remaining: int
    status: str
    reason: str
    error: str = ""


@dataclass(frozen=True)
class EvidenceCandidateDraft:
    id: str
    source: str
    evidence_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    title: str
    rationale: str
    proposed_change: str
    expected_benefit: str
    risk: str
    test_plan: str
    score: int


@dataclass(frozen=True)
class _ConversationTurn:
    ref: str
    occurred_at: str
    message: str
    reply: str


@dataclass(frozen=True)
class _TaskSnapshot:
    task_id: int
    marker: str
    occurred_at: str
    refs: tuple[str, ...]
    payload: dict[str, object]


def evidence_settings_path(root: Path | None = None) -> Path:
    return private_state_path("evidence.json", root)


def evidence_index_path(root: Path | None = None) -> Path:
    return artifact_path("evidence.jsonl", root)


def evidence_scan_index_path(root: Path | None = None) -> Path:
    return artifact_path("evidence_scans.jsonl", root)


def load_evidence_settings(root: Path | None = None) -> EvidenceSettings:
    raw = load_json_object(evidence_settings_path(root))
    return EvidenceSettings(
        feedback_batch_size=_batch_size(
            raw.get("feedback_batch_size"),
            default=DEFAULT_FEEDBACK_BATCH_SIZE,
        ),
        experience_batch_size=_batch_size(
            raw.get("experience_batch_size"),
            default=DEFAULT_EXPERIENCE_BATCH_SIZE,
        ),
    )


def save_evidence_batch_size(
    source: str,
    batch_size: int | str,
    root: Path | None = None,
) -> EvidenceSettings:
    normalized_source = _source(source)
    normalized_size = _validated_batch_size(batch_size)
    path = evidence_settings_path(root)
    with file_transaction(path):
        current = load_evidence_settings(root)
        settings = EvidenceSettings(
            feedback_batch_size=(
                normalized_size
                if normalized_source == "feedback"
                else current.feedback_batch_size
            ),
            experience_batch_size=(
                normalized_size
                if normalized_source == "experience"
                else current.experience_batch_size
            ),
        )
        atomic_write(
            path,
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    **asdict(settings),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    return settings


def load_evidence(
    root: Path | None = None,
    *,
    source: str = "",
    include_inactive: bool = True,
    limit: int = 5000,
) -> tuple[EvidenceSignal, ...]:
    if limit <= 0:
        return ()
    wanted_source = _source(source) if clean_text(source) else ""
    latest: dict[str, EvidenceSignal] = {}
    order: list[str] = []
    for raw in _jsonl_objects("evidence.jsonl", root):
        signal = _signal_from_json(raw)
        if signal is None:
            continue
        if signal.id not in latest:
            order.append(signal.id)
        latest[signal.id] = signal
    signals = [
        latest[signal_id]
        for signal_id in order
        if signal_id in latest
        and (not wanted_source or latest[signal_id].source == wanted_source)
        and (include_inactive or latest[signal_id].status == "active")
    ]
    return tuple(signals[-limit:])


def load_evidence_scans(
    root: Path | None = None,
    *,
    source: str = "",
    limit: int = 5000,
) -> tuple[EvidenceScanRecord, ...]:
    if limit <= 0:
        return ()
    wanted_source = _source(source) if clean_text(source) else ""
    records: list[EvidenceScanRecord] = []
    for raw in _jsonl_objects("evidence_scans.jsonl", root):
        record = _scan_from_json(raw)
        if record is None or (wanted_source and record.source != wanted_source):
            continue
        records.append(record)
    return tuple(records[-limit:])


def pending_evidence_counts(root: Path | None = None) -> dict[str, int]:
    scanned_feedback = {
        ref
        for scan in load_evidence_scans(root, source="feedback")
        if scan.status == "completed"
        for ref in scan.input_refs
    }
    task_markers = _scanned_task_markers(root)
    return {
        "feedback": sum(
            turn.ref not in scanned_feedback
            for turn in _conversation_turns(root)
        ),
        "experience": sum(
            task_markers.get(snapshot.task_id) != snapshot.marker
            for snapshot in _task_snapshots(root)
        ),
    }


def scan_evidence(
    source: str,
    root: Path | None = None,
    *,
    generator: EvidenceGenerator,
    force: bool = False,
    reason: str = "threshold",
) -> EvidenceScanResult:
    normalized_source = _source(source)
    settings = load_evidence_settings(root)
    batch_size = (
        settings.feedback_batch_size
        if normalized_source == "feedback"
        else settings.experience_batch_size
    )
    if normalized_source == "feedback":
        return _scan_feedback(
            root,
            generator=generator,
            batch_size=batch_size,
            force=force,
            reason=reason,
        )
    return _scan_experience(
        root,
        generator=generator,
        batch_size=batch_size,
        force=force,
        reason=reason,
    )


def unlinked_evidence(
    root: Path | None = None,
    *,
    limit: int = 100,
) -> tuple[EvidenceSignal, ...]:
    return tuple(
        signal
        for signal in load_evidence(root, include_inactive=False, limit=limit)
        if not signal.candidate_ids
    )


def synthesize_evidence_candidates(
    evidence: Iterable[EvidenceSignal],
    *,
    mission: str,
    theme: str,
    existing_candidates: Iterable[Mapping[str, object]],
    generator: EvidenceGenerator,
    limit: int = MAX_CANDIDATES_PER_SYNTHESIS,
) -> tuple[EvidenceCandidateDraft, ...]:
    signals = tuple(evidence)
    if not signals:
        return ()
    bounded_limit = max(1, min(int(limit), MAX_CANDIDATES_PER_SYNTHESIS))
    prompt = evidence_candidate_prompt(
        signals,
        mission=mission,
        theme=theme,
        existing_candidates=existing_candidates,
        limit=bounded_limit,
    )
    payload = _json_array(generator(prompt))
    if not isinstance(payload, list):
        raise EvidenceError("evidence candidate synthesizer returned malformed JSON")
    known = {signal.id: signal for signal in signals}
    drafts: list[EvidenceCandidateDraft] = []
    used_groups: set[tuple[str, ...]] = set()
    for raw in payload[:bounded_limit]:
        if not isinstance(raw, dict) or set(raw) != {
            "evidence_ids",
            "title",
            "rationale",
            "proposed_change",
            "expected_benefit",
            "risk",
            "test_plan",
        }:
            raise EvidenceError("evidence candidate synthesizer returned an invalid schema")
        evidence_ids = _response_string_tuple(raw.get("evidence_ids"))
        if not evidence_ids or any(evidence_id not in known for evidence_id in evidence_ids):
            raise EvidenceError("evidence candidate references unknown evidence")
        sources = {known[evidence_id].source for evidence_id in evidence_ids}
        if len(sources) != 1:
            raise EvidenceError("one candidate cannot combine different evidence sources")
        fields = {
            key: _required_text(raw, key, limit=1200)
            for key in (
                "title",
                "rationale",
                "proposed_change",
                "expected_benefit",
                "risk",
                "test_plan",
            )
        }
        if not candidate_scope_is_safe(fields):
            raise EvidenceError("evidence candidate contains protected or dangerous scope")
        group = tuple(sorted(set(evidence_ids)))
        if group in used_groups:
            continue
        used_groups.add(group)
        source = sources.pop()
        refs = _dedupe(
            ref
            for evidence_id in group
            for ref in known[evidence_id].evidence_refs
        )
        explicit_bonus = 4 if any(known[evidence_id].explicit for evidence_id in group) else 0
        confidence = sum(known[evidence_id].confidence for evidence_id in group) / len(group)
        drafts.append(
            EvidenceCandidateDraft(
                id=_candidate_id(source, group, fields["title"]),
                source=source,
                evidence_ids=group,
                evidence_refs=refs,
                score=round(confidence * 20) + explicit_bonus,
                **fields,
            )
        )
    return tuple(drafts)


def evidence_candidate_prompt(
    evidence: Iterable[EvidenceSignal],
    *,
    mission: str,
    theme: str,
    existing_candidates: Iterable[Mapping[str, object]],
    limit: int = MAX_CANDIDATES_PER_SYNTHESIS,
) -> str:
    signals = tuple(evidence)
    existing = [
        {
            "id": clean_text(str(candidate.get("id") or "")),
            "source": clean_text(str(candidate.get("source") or "")),
            "title": _bounded_prompt_text(candidate.get("title"), limit=300),
            "status": clean_text(str(candidate.get("status") or "")),
        }
        for candidate in existing_candidates
    ]
    payload = {
        "mission": _bounded_prompt_text(mission, limit=1500),
        "theme": _bounded_prompt_text(theme, limit=500),
        "evidence": [asdict(signal) for signal in signals],
        "existing_candidates": existing,
    }
    schema = [
        {
            "evidence_ids": ["existing evidence ID"],
            "title": "short candidate title",
            "rationale": "why the cited evidence supports a durable Enoch improvement",
            "proposed_change": "small reversible change to Enoch's body or workflow",
            "expected_benefit": "specific observable benefit",
            "risk": "specific bounded risk",
            "test_plan": "specific verification plan",
        }
    ]
    return "\n".join(
        [
            "Synthesize possible self-evolution candidates from validated evidence.",
            "Return exactly one JSON array and no prose.",
            f"Return at most {max(1, min(limit, MAX_CANDIDATES_PER_SYNTHESIS))} candidates.",
            "Returning an empty array is valid when no evidence supports a durable actionable change.",
            "Reference only supplied evidence IDs and do not combine feedback and experience in one candidate.",
            "Do not duplicate an existing candidate.",
            "Candidates must improve Enoch itself, not merely repeat ordinary user work.",
            "Keep every change small, reversible, testable, and grounded in the cited evidence.",
            "Do not change identity, mission, secrets, credentials, permissions, merge authority, deployment, or daemon configuration.",
            f"Required response schema: {json.dumps(schema, sort_keys=True)}",
            f"Synthesis input: {json.dumps(payload, sort_keys=True)}",
        ]
    )


def link_evidence(
    evidence_ids: Iterable[str],
    candidate_id: str,
    root: Path | None = None,
) -> tuple[EvidenceSignal, ...]:
    wanted = {clean_text(value) for value in evidence_ids if clean_text(value)}
    candidate_id = clean_text(candidate_id)
    if not wanted or not candidate_id:
        return ()
    updates: list[EvidenceSignal] = []
    for signal in load_evidence(root):
        if signal.id not in wanted:
            continue
        candidate_ids = _dedupe((*signal.candidate_ids, candidate_id))
        updates.append(
            EvidenceSignal(
                **{
                    **signal.__dict__,
                    "updated_at": current_time(),
                    "status": "linked",
                    "candidate_ids": candidate_ids,
                }
            )
        )
    _append_jsonl(evidence_index_path(root), (asdict(signal) for signal in updates))
    return tuple(updates)


def _scan_feedback(
    root: Path | None,
    *,
    generator: EvidenceGenerator,
    batch_size: int,
    force: bool,
    reason: str,
) -> EvidenceScanResult:
    scanned = {
        ref
        for scan in load_evidence_scans(root, source="feedback")
        if scan.status == "completed"
        for ref in scan.input_refs
    }
    pending = tuple(turn for turn in _conversation_turns(root) if turn.ref not in scanned)
    if not pending:
        return EvidenceScanResult("feedback", 0, (), 0, "empty", reason)
    if len(pending) < batch_size and not force:
        return EvidenceScanResult("feedback", 0, (), len(pending), "waiting", reason)
    batch = pending[:batch_size]
    input_refs = tuple(turn.ref for turn in batch)
    prompt_input = [
        {
            "ref": turn.ref,
            "occurred_at": turn.occurred_at,
            "user_message": _redact_record_text(turn.message),
            "enoch_reply": _redact_record_text(turn.reply),
        }
        for turn in batch
    ]
    return _run_scan(
        "feedback",
        root,
        generator=generator,
        prompt_input=prompt_input,
        known_refs=set(input_refs),
        input_refs=input_refs,
        task_markers={},
        processed=len(batch),
        remaining=max(0, len(pending) - len(batch)),
        reason=reason,
    )


def _scan_experience(
    root: Path | None,
    *,
    generator: EvidenceGenerator,
    batch_size: int,
    force: bool,
    reason: str,
) -> EvidenceScanResult:
    scanned = _scanned_task_markers(root)
    pending = tuple(
        snapshot
        for snapshot in _task_snapshots(root)
        if scanned.get(snapshot.task_id) != snapshot.marker
    )
    if not pending:
        return EvidenceScanResult("experience", 0, (), 0, "empty", reason)
    if len(pending) < batch_size and not force:
        return EvidenceScanResult("experience", 0, (), len(pending), "waiting", reason)
    batch = pending[:batch_size]
    task_markers = {snapshot.task_id: snapshot.marker for snapshot in batch}
    input_refs = tuple(
        ref
        for snapshot in batch
        for ref in snapshot.refs
    )
    return _run_scan(
        "experience",
        root,
        generator=generator,
        prompt_input=[snapshot.payload for snapshot in batch],
        known_refs=set(input_refs),
        input_refs=input_refs,
        task_markers=task_markers,
        processed=len(batch),
        remaining=max(0, len(pending) - len(batch)),
        reason=reason,
    )


def _run_scan(
    source: str,
    root: Path | None,
    *,
    generator: EvidenceGenerator,
    prompt_input: list[dict[str, object]],
    known_refs: set[str],
    input_refs: tuple[str, ...],
    task_markers: dict[int, str],
    processed: int,
    remaining: int,
    reason: str,
) -> EvidenceScanResult:
    scan_id = f"evidence-scan-{uuid4().hex}"
    try:
        response = generator(evidence_scan_prompt(source, prompt_input))
        signals = _parse_evidence_response(
            response,
            source=source,
            known_refs=known_refs,
        )
        existing_ids = {signal.id for signal in load_evidence(root)}
        new_signals = tuple(signal for signal in signals if signal.id not in existing_ids)
        _append_jsonl(
            evidence_index_path(root),
            (asdict(signal) for signal in new_signals),
        )
        record = EvidenceScanRecord(
            id=scan_id,
            source=source,
            created_at=current_time(),
            status="completed",
            reason=clean_text(reason),
            input_refs=input_refs,
            task_markers=task_markers,
            evidence_ids=tuple(signal.id for signal in signals),
        )
        _append_jsonl(evidence_scan_index_path(root), (asdict(record),))
        return EvidenceScanResult(
            source,
            processed,
            new_signals,
            remaining,
            "completed",
            reason,
        )
    except (EvidenceError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
        detail = clean_text(str(error)) or error.__class__.__name__
        record = EvidenceScanRecord(
            id=scan_id,
            source=source,
            created_at=current_time(),
            status="failed",
            reason=clean_text(reason),
            input_refs=input_refs,
            task_markers={},
            evidence_ids=(),
            error=detail[:1000],
        )
        _append_jsonl(evidence_scan_index_path(root), (asdict(record),))
        return EvidenceScanResult(
            source,
            0,
            (),
            processed + remaining,
            "failed",
            reason,
            detail,
        )


def evidence_scan_prompt(source: str, records: list[dict[str, object]]) -> str:
    source_guidance = (
        "The records are verbatim user messages paired with Enoch's replies. "
        "Use the replies to resolve references, but treat the user's words as the feedback signal."
        if source == "feedback"
        else (
            "The records are complete task lifecycle snapshots. Look for durable operational "
            "friction, missing capabilities, unsafe recovery, regressions, or repeated human intervention."
        )
    )
    schema = [
        {
            "observation": "what the records demonstrate",
            "evidence_type": "short semantic category",
            "affected_area": "Enoch subsystem or workflow",
            "desired_outcome": "observable improvement, without prescribing implementation",
            "confidence": "number from 0.0 to 1.0",
            "explicit": "boolean; true only when a human stated it directly",
            "evidence_refs": ["one or more supplied record refs"],
        }
    ]
    return "\n".join(
        [
            f"Extract possible {source} evidence for improving Enoch's own body or operating workflow.",
            "Return exactly one JSON array and no prose.",
            f"Return at most {MAX_SIGNALS_PER_SCAN} evidence signals.",
            "Returning an empty array is correct when the records contain no improvement evidence.",
            source_guidance,
            "Describe observations and desired outcomes only. Do not propose code changes, candidates, tasks, risks, or test plans.",
            "Do not treat ordinary requested work, successful completion, or an active schedule as evidence by itself.",
            "Reference only refs supplied in the input and never invent facts or identifiers.",
            f"Required response schema: {json.dumps(schema, sort_keys=True)}",
            f"Evidence input: {json.dumps(records, sort_keys=True)}",
        ]
    )


def _parse_evidence_response(
    response: str,
    *,
    source: str,
    known_refs: set[str],
) -> tuple[EvidenceSignal, ...]:
    payload = _json_array(response)
    if not isinstance(payload, list):
        raise EvidenceError("evidence scanner returned malformed JSON")
    created_at = current_time()
    signals: list[EvidenceSignal] = []
    for raw in payload[:MAX_SIGNALS_PER_SCAN]:
        if not isinstance(raw, dict) or set(raw) != {
            "observation",
            "evidence_type",
            "affected_area",
            "desired_outcome",
            "confidence",
            "explicit",
            "evidence_refs",
        }:
            raise EvidenceError("evidence scanner returned an invalid schema")
        refs = _response_string_tuple(raw.get("evidence_refs"))
        if not refs or any(ref not in known_refs for ref in refs):
            raise EvidenceError("evidence scanner cited an unknown record")
        confidence = _confidence(raw.get("confidence"))
        explicit = raw.get("explicit")
        if not isinstance(explicit, bool):
            raise EvidenceError("evidence explicit flag must be boolean")
        fields = {
            key: _required_text(raw, key, limit=2000)
            for key in (
                "observation",
                "evidence_type",
                "affected_area",
                "desired_outcome",
            )
        }
        signal_id = _signal_id(source, fields["observation"], refs)
        signals.append(
            EvidenceSignal(
                id=signal_id,
                source=source,
                confidence=confidence,
                explicit=explicit,
                evidence_refs=refs,
                created_at=created_at,
                updated_at=created_at,
                **fields,
            )
        )
    return tuple(signals)


def _conversation_turns(root: Path | None) -> tuple[_ConversationTurn, ...]:
    turns: dict[str, _ConversationTurn] = {}
    paths = {
        str(path.resolve()): path
        for directory in conversation_log_dirs(root)
        for path in directory.glob("*.jsonl")
    }
    for path in sorted(paths.values()):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            message = str(raw.get("message") or "")
            if not message.strip():
                continue
            record_id = clean_text(str(raw.get("id") or ""))
            if record_id:
                ref = f"conversation:{record_id}"
            else:
                canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
                ref = f"conversation:legacy-{digest}"
            turns.setdefault(
                ref,
                _ConversationTurn(
                    ref=ref,
                    occurred_at=str(raw.get("time") or ""),
                    message=message,
                    reply=str(raw.get("reply") or ""),
                ),
            )
    return tuple(
        sorted(
            turns.values(),
            key=lambda turn: (turn.occurred_at, turn.ref),
        )
    )


def _task_snapshots(root: Path | None) -> tuple[_TaskSnapshot, ...]:
    grouped: dict[int, list[TaskEvent]] = {}
    for event in load_task_events(root):
        grouped.setdefault(event.task_id, []).append(event)
    snapshots: list[_TaskSnapshot] = []
    for task_id, events in grouped.items():
        # load_task_events preserves append order. Keep that order because
        # multiple lifecycle events can share the same second, while their
        # random IDs do not encode which event happened last.
        latest = events[-1]
        refs = _dedupe(
            (
                f"task:{task_id}",
                *(f"task-event:{event.id}" for event in events),
            )
        )
        snapshots.append(
            _TaskSnapshot(
                task_id=task_id,
                marker=latest.id,
                occurred_at=latest.occurred_at,
                refs=refs,
                payload={
                    "task_id": task_id,
                    "task_ref": f"task:{task_id}",
                    "latest_event_ref": f"task-event:{latest.id}",
                    "events": [_task_event_payload(event) for event in events],
                },
            )
        )
    return tuple(
        sorted(
            snapshots,
            key=lambda snapshot: (snapshot.occurred_at, snapshot.task_id),
        )
    )


def _task_event_payload(event: TaskEvent) -> dict[str, object]:
    return {
        "ref": f"task-event:{event.id}",
        "occurred_at": event.occurred_at,
        "event": event.event,
        "source": event.source,
        "initiated_by": event.initiated_by,
        "event_actor": event.event_actor,
        "trigger": event.trigger,
        "request": _redact_record_text(event.request),
        "result_summary": _redact_record_text(event.result_summary),
        "context_source": event.context_source,
        "candidate_id": event.candidate_id,
        "parent_task_id": event.parent_task_id,
        "evidence_source": event.evidence_source,
        "signal_actor": event.signal_actor,
        "candidate_actor": event.candidate_actor,
        "approval_actor": event.approval_actor,
        "parent_candidate_id": event.parent_candidate_id,
        "source_task_id": event.source_task_id,
        "related_task_id": event.related_task_id,
        "pr_urls": list(event.pr_urls),
        "changed_files": list(event.changed_files),
        "publish_stage": event.publish_stage,
        "commit_sha": event.commit_sha,
        "attempt": event.attempt,
        "max_attempts": event.max_attempts,
        "next_attempt_at": event.next_attempt_at,
        "failure_code": event.failure_code,
        "failure_class": event.failure_class,
        "retryable": event.retryable,
        "runtime_provider": event.runtime_provider,
        "runtime_completion_reason": event.runtime_completion_reason,
        "runtime_usage": event.runtime_usage,
        "runtime_event_types": list(event.runtime_event_types),
        "runtime_output_refs": [
            _redact_record_text(value)
            for value in event.runtime_output_refs
        ],
        "runtime_side_effects": list(event.runtime_side_effects),
    }


def _scanned_task_markers(root: Path | None) -> dict[int, str]:
    markers: dict[int, str] = {}
    for scan in load_evidence_scans(root, source="experience"):
        if scan.status == "completed":
            markers.update(scan.task_markers)
    return markers


def _append_jsonl(path: Path, records: Iterable[Mapping[str, object]]) -> None:
    payloads = tuple(records)
    if not payloads:
        return
    with file_transaction(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(
                    json.dumps(
                        {"schema_version": SCHEMA_VERSION, **dict(payload)},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )


def _jsonl_objects(relative: str, root: Path | None) -> tuple[dict[str, object], ...]:
    objects: list[dict[str, object]] = []
    seen_lines: set[str] = set()
    for path in artifact_read_paths(relative, root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip() or line in seen_lines:
                continue
            seen_lines.add(line)
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                objects.append(raw)
    return tuple(objects)


def _signal_from_json(raw: Mapping[str, object]) -> EvidenceSignal | None:
    signal_id = clean_text(str(raw.get("id") or ""))
    source = clean_text(str(raw.get("source") or "")).lower()
    observation = clean_text(str(raw.get("observation") or ""))
    if not signal_id or source not in EVIDENCE_SOURCES or not observation:
        return None
    status = clean_text(str(raw.get("status") or "active")).lower()
    return EvidenceSignal(
        id=signal_id,
        source=source,
        observation=observation,
        evidence_type=clean_text(str(raw.get("evidence_type") or "observation")),
        affected_area=clean_text(str(raw.get("affected_area") or "Enoch workflow")),
        desired_outcome=clean_text(str(raw.get("desired_outcome") or "")),
        confidence=_loaded_confidence(raw.get("confidence")),
        explicit=bool(raw.get("explicit", False)),
        evidence_refs=_loaded_string_tuple(raw.get("evidence_refs")),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or raw.get("created_at") or ""),
        status=status if status in EVIDENCE_STATUSES else "active",
        candidate_ids=_loaded_string_tuple(raw.get("candidate_ids")),
    )


def _scan_from_json(raw: Mapping[str, object]) -> EvidenceScanRecord | None:
    scan_id = clean_text(str(raw.get("id") or ""))
    source = clean_text(str(raw.get("source") or "")).lower()
    status = clean_text(str(raw.get("status") or "")).lower()
    if not scan_id or source not in EVIDENCE_SOURCES or status not in {"completed", "failed"}:
        return None
    raw_markers = raw.get("task_markers")
    markers: dict[int, str] = {}
    if isinstance(raw_markers, dict):
        for task_id, marker in raw_markers.items():
            parsed_id = _positive_int(task_id)
            cleaned_marker = clean_text(str(marker or ""))
            if parsed_id is not None and cleaned_marker:
                markers[parsed_id] = cleaned_marker
    return EvidenceScanRecord(
        id=scan_id,
        source=source,
        created_at=str(raw.get("created_at") or ""),
        status=status,
        reason=clean_text(str(raw.get("reason") or "")),
        input_refs=_loaded_string_tuple(raw.get("input_refs")),
        task_markers=markers,
        evidence_ids=_loaded_string_tuple(raw.get("evidence_ids")),
        error=clean_text(str(raw.get("error") or "")),
    )


def _json_array(response: str) -> object:
    stripped = str(response or "").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _required_text(
    raw: Mapping[str, object],
    key: str,
    *,
    limit: int,
) -> str:
    value = clean_text(str(raw.get(key) or ""))
    if not value:
        raise EvidenceError(f"evidence response requires {key}")
    return value[:limit]


def _response_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EvidenceError("evidence references must be a JSON array")
    output = _loaded_string_tuple(value)
    if len(output) != len(value):
        raise EvidenceError("evidence references contain an empty or duplicate value")
    return output


def _loaded_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return _dedupe(clean_text(str(item or "")) for item in value if clean_text(str(item or "")))


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise EvidenceError("evidence confidence must be a number")
    try:
        confidence = float(value)
    except (TypeError, ValueError) as error:
        raise EvidenceError("evidence confidence must be a number") from error
    if confidence < 0.0 or confidence > 1.0:
        raise EvidenceError("evidence confidence must be between 0 and 1")
    return round(confidence, 4)


def _loaded_confidence(value: object) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _signal_id(source: str, observation: str, refs: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "source": source,
            "observation": clean_text(observation).casefold(),
            "refs": sorted(refs),
        },
        sort_keys=True,
    )
    return f"evidence-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _candidate_id(source: str, evidence_ids: tuple[str, ...], title: str) -> str:
    payload = json.dumps(
        {
            "source": source,
            "evidence_ids": evidence_ids,
            "title": clean_text(title).casefold(),
        },
        sort_keys=True,
    )
    return f"{source}-evidence-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _redact_record_text(value: object) -> str:
    text = str(value or "")
    text = _SECRET_PATTERN.sub(r"\1=[redacted]", text)
    text = _BEARER_PATTERN.sub("Bearer [redacted]", text)
    text = _TELEGRAM_TOKEN_PATTERN.sub("[telegram-token-redacted]", text)
    text = _SETUP_TOKEN_PATTERN.sub(r"\1[redacted]", text)
    return _KNOWN_CREDENTIAL_PATTERN.sub("[credential-redacted]", text)


def _bounded_prompt_text(value: object, *, limit: int) -> str:
    return clean_text(_redact_record_text(value))[:limit]


def _source(value: str) -> str:
    source = clean_text(value).lower()
    if source not in EVIDENCE_SOURCES:
        raise ValueError("Evidence source must be feedback or experience.")
    return source


def _validated_batch_size(value: object) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Evidence batch size must be a number.") from error
    if size < MIN_BATCH_SIZE or size > MAX_BATCH_SIZE:
        raise ValueError(
            f"Evidence batch size must be between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE}."
        )
    return size


def _batch_size(value: object, *, default: int) -> int:
    try:
        return _validated_batch_size(int(value))
    except (TypeError, ValueError):
        return default


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_text(str(value or ""))
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return tuple(output)
