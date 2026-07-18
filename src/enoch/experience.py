from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None

from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import enoch_home
from enoch.task_queue import TaskJob


SCHEMA_VERSION = 1
SUMMARY_LIMIT = 4000
TERMINAL_OUTCOMES = {"completed", "failed", "cancelled"}
_EXPERIENCE_THREAD_LOCK = threading.RLock()


@dataclass(frozen=True)
class ExperienceRecord:
    id: str
    task_id: int
    created_at: str
    command: str
    outcome: str
    request: str
    result_summary: str
    context_source: str
    pr_urls: tuple[str, ...]
    changed_files: tuple[str, ...]
    started: bool


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
        raise ValueError("Experience can only record completed, failed, or cancelled tasks.")
    request = clean_text(job.text)
    if not request:
        raise ValueError("Experience task request is required.")
    record = ExperienceRecord(
        id=f"task-{job.id}",
        task_id=job.id,
        created_at=job.completed_at or current_time(),
        command=command.strip(),
        outcome=outcome,
        request=request,
        result_summary=_clip(result or job.result),
        context_source=job.context_source.strip(),
        pr_urls=_dedupe(job.pr_urls),
        changed_files=_changed_files(result or job.result),
        started=bool(job.started_at),
    )
    with _experience_transaction(root):
        existing = _record_for_task(job.id, root)
        if existing is not None:
            return existing
        path = experience_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"schema_version": SCHEMA_VERSION, **asdict(record)}, sort_keys=True) + "\n")
    return record


def load_experience_records(
    root: Path | None = None,
    *,
    limit: int = 200,
) -> tuple[ExperienceRecord, ...]:
    path = experience_path(root)
    if not path.exists() or limit <= 0:
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    records: list[ExperienceRecord] = []
    seen: set[int] = set()
    for line in reversed(lines):
        record = _record_from_line(line)
        if record is None or record.task_id in seen:
            continue
        seen.add(record.task_id)
        records.append(record)
        if len(records) >= limit:
            break
    return tuple(records)


def _record_for_task(task_id: int, root: Path | None) -> ExperienceRecord | None:
    return next(
        (record for record in load_experience_records(root, limit=1_000_000) if record.task_id == task_id),
        None,
    )


def _record_from_line(line: str) -> ExperienceRecord | None:
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
    return ExperienceRecord(
        id=clean_text(str(raw.get("id") or "")) or f"task-{task_id}",
        task_id=task_id,
        created_at=str(raw.get("created_at") or ""),
        command=str(raw.get("command") or "").strip(),
        outcome=outcome,
        request=request,
        result_summary=str(raw.get("result_summary") or "").strip(),
        context_source=str(raw.get("context_source") or "").strip(),
        pr_urls=_string_tuple(raw.get("pr_urls")),
        changed_files=_string_tuple(raw.get("changed_files")),
        started=bool(raw.get("started", False)),
    )


def _changed_files(result: str) -> tuple[str, ...]:
    files: list[str] = []
    in_files = False
    for raw_line in result.splitlines():
        line = raw_line.strip()
        if line == "Files:":
            in_files = True
            continue
        if in_files and line.startswith("- "):
            files.append(line[2:].strip())
            continue
        if in_files and line:
            in_files = False
    return _dedupe(files)


def _clip(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= SUMMARY_LIMIT:
        return cleaned
    return f"{cleaned[:SUMMARY_LIMIT].rstrip()}\n\n[truncated]"


def _dedupe(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return tuple(output)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return _dedupe([str(item) for item in value])


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@contextmanager
def _experience_transaction(root: Path | None = None):
    path = experience_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _EXPERIENCE_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
