"""Private, bounded ledger of runtime token usage.

Every runtime call Enoch makes (task workers, CI fix tasks, conversation
turns, task-context snapshots, evolve/evidence turns, and anything else)
appends one small JSON line to ``.enoch/usage/<local-date>.jsonl``. A record
holds only a timestamp, a category, an optional task id, the provider and
model names, and token counts. It never holds prompt or response text: the
writer accepts token counts only.

The ledger is bounded. Raw day files are kept for ``RAW_RETENTION_DAYS``;
older days are folded into ``summary.json`` (per-day, per-category totals)
and deleted, and summaries older than ``SUMMARY_RETENTION_DAYS`` are dropped.
A single day file stops growing at ``MAX_DAY_FILE_BYTES``.

Nothing here calls a model, the forge, or the network, and every public
function swallows its own errors: usage visibility must never break a turn.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import threading
from typing import Any

from enoch.paths import private_state_path
from enoch.state import atomic_write


USAGE_DIRECTORY = "usage"
SUMMARY_FILE = "summary.json"
SUMMARY_SCHEMA_VERSION = 1
RAW_RETENTION_DAYS = 14
SUMMARY_RETENTION_DAYS = 180
MAX_DAY_FILE_BYTES = 16 * 1024 * 1024
LABEL_LIMIT = 64

CATEGORY_TASK = "task"
CATEGORY_CI_FIX = "ci-fix"
CATEGORY_CONVERSATION = "conversation"
CATEGORY_SNAPSHOT = "snapshot"
CATEGORY_EVOLVE = "evolve"
CATEGORY_OTHER = "other"
CATEGORIES = (
    CATEGORY_TASK,
    CATEGORY_CI_FIX,
    CATEGORY_CONVERSATION,
    CATEGORY_SNAPSHOT,
    CATEGORY_EVOLVE,
    CATEGORY_OTHER,
)
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
)
# The board groups categories into three shares to stay on one line.
BOARD_GROUPS = (
    ("tasks", (CATEGORY_TASK, CATEGORY_CI_FIX)),
    ("chat", (CATEGORY_CONVERSATION,)),
    ("other", (CATEGORY_SNAPSHOT, CATEGORY_EVOLVE, CATEGORY_OTHER)),
)
TOP_TASKS = 5

_WRITE_LOCK = threading.Lock()
_ROTATED_DAYS: dict[str, str] = {}
_REPORT_CACHE: dict[tuple[str, int], tuple[tuple, "UsageReport"]] = {}


@dataclass
class UsageTotals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0

    def add(self, other: Mapping[str, Any] | "UsageTotals", *, calls: int = 1) -> None:
        values = other if isinstance(other, Mapping) else other.__dict__
        for name in TOKEN_FIELDS:
            setattr(self, name, getattr(self, name) + _count(values.get(name)))
        if isinstance(other, UsageTotals):
            self.calls += other.calls
        elif "calls" in values:
            self.calls += _count(values.get("calls"))
        else:
            self.calls += calls

    @property
    def empty(self) -> bool:
        return self.input_tokens == 0 and self.output_tokens == 0

    def as_dict(self) -> dict[str, int]:
        return {**{name: getattr(self, name) for name in TOKEN_FIELDS}, "calls": self.calls}


@dataclass(frozen=True)
class UsageReport:
    start_day: str
    end_day: str
    total: UsageTotals = field(default_factory=UsageTotals)
    by_category: dict[str, UsageTotals] = field(default_factory=dict)
    by_task: dict[int, UsageTotals] = field(default_factory=dict)


def usage_directory(root: Path | None = None) -> Path:
    return private_state_path(USAGE_DIRECTORY, root)


def day_file(day: date, root: Path | None = None) -> Path:
    return usage_directory(root) / f"{day.isoformat()}.jsonl"


def normalize_category(category: str) -> str:
    cleaned = str(category or "").strip().lower()
    return cleaned if cleaned in CATEGORIES else CATEGORY_OTHER


def record_runtime_usage(
    root: Path | None,
    *,
    category: str,
    usage: object,
    provider: str = "",
    model: str = "",
    task_id: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Append one usage record. Returns False (never raises) on any failure.

    ``usage`` is a ``RuntimeUsage``-like object or a mapping of token counts;
    missing counts are recorded as zero.
    """

    try:
        moment = _local(now)
        record = {
            "ts": moment.isoformat(timespec="seconds"),
            "category": normalize_category(category),
            "task_id": _task_id(task_id),
            "provider": _label(provider),
            "model": _label(model),
            **{name: _usage_count(usage, name) for name in TOKEN_FIELDS},
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        path = day_file(moment.date(), root)
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            first_of_day = not path.exists()
            if not first_of_day and path.stat().st_size >= MAX_DAY_FILE_BYTES:
                return False
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        if first_of_day:
            rotate_usage_ledger(root, today=moment.date())
        return True
    except Exception:
        return False


def rotate_usage_ledger(root: Path | None, *, today: date | None = None) -> bool:
    """Fold raw day files older than the retention window into the summary.

    Runs at most once per day per ledger in a process. Returns False (never
    raises) on failure.
    """

    try:
        today = today or _local(None).date()
        directory = usage_directory(root)
        key = str(directory)
        if _ROTATED_DAYS.get(key) == today.isoformat():
            return True
        if not directory.is_dir():
            _ROTATED_DAYS[key] = today.isoformat()
            return True
        raw_cutoff = today - timedelta(days=RAW_RETENTION_DAYS - 1)
        summary_cutoff = today - timedelta(days=SUMMARY_RETENTION_DAYS - 1)
        with _WRITE_LOCK:
            expired = sorted(
                (day, path)
                for path in directory.glob("*.jsonl")
                if (day := _file_day(path)) is not None and day < raw_cutoff
            )
            summary_path = directory / SUMMARY_FILE
            summary = _load_summary(summary_path)
            days = summary["days"]
            for day, path in expired:
                if day >= summary_cutoff:
                    totals = _day_category_totals(path)
                    if totals:
                        merged = days.setdefault(day.isoformat(), {})
                        for category, values in totals.items():
                            existing = UsageTotals()
                            existing.add(merged.get(category, {}), calls=0)
                            existing.add(values)
                            merged[category] = existing.as_dict()
            pruned = False
            for day_key in list(days):
                parsed = _parse_day(day_key)
                if parsed is None or parsed < summary_cutoff:
                    days.pop(day_key, None)
                    pruned = True
            if expired or pruned:
                atomic_write(
                    summary_path,
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                )
            for _day, path in expired:
                path.unlink(missing_ok=True)
        _ROTATED_DAYS[key] = today.isoformat()
        return True
    except Exception:
        return False


def load_usage_report(
    root: Path | None,
    *,
    days: int = 1,
    now: datetime | None = None,
) -> UsageReport:
    """Aggregate the last ``days`` local days (today included).

    Unreadable files and malformed lines are skipped. The result is cached
    by the files' sizes and modification times, so repeated board refreshes
    re-read nothing until a new record lands.
    """

    days = max(1, int(days))
    end = _local(now).date()
    start = end - timedelta(days=days - 1)
    paths = [day_file(start + timedelta(days=offset), root) for offset in range(days)]
    try:
        signature = tuple(_stat_signature(path) for path in paths)
    except Exception:
        signature = ()
    cache_key = (str(usage_directory(root)), days)
    cached = _REPORT_CACHE.get(cache_key)
    if signature and cached is not None and cached[0] == (end, signature):
        return cached[1]
    report = UsageReport(start_day=start.isoformat(), end_day=end.isoformat())
    for record in _iter_records(paths):
        totals = _totals_from_record(record)
        category = normalize_category(str(record.get("category") or ""))
        report.total.add(totals)
        report.by_category.setdefault(category, UsageTotals()).add(totals)
        task_id = _task_id(record.get("task_id"))
        if task_id is not None:
            report.by_task.setdefault(task_id, UsageTotals()).add(totals)
    if signature:
        _REPORT_CACHE[cache_key] = ((end, signature), report)
    return report


def task_usage(
    root: Path | None,
    task_id: int,
    *,
    since: str | date | datetime | None = None,
) -> UsageTotals:
    """Sum every recorded call for ``task_id`` across the raw retention window.

    ``since`` (the task's creation time) skips day files before that day, so
    a final message reads only the days the task could have run on.
    """

    totals = UsageTotals()
    try:
        directory = usage_directory(root)
        if not directory.is_dir():
            return totals
        first_day = _since_day(since)
        paths = sorted(
            path
            for path in directory.glob("*.jsonl")
            if (day := _file_day(path)) is not None and (first_day is None or day >= first_day)
        )
        for record in _iter_records(paths):
            if _task_id(record.get("task_id")) == task_id:
                totals.add(_totals_from_record(record))
    except Exception:
        return UsageTotals()
    return totals


# Formatting -----------------------------------------------------------------


def format_tokens(value: int) -> str:
    """Compact token count: 950, 62k, 8.1M, 150M."""

    value = max(0, int(value))
    if value < 1_000:
        return str(value)
    if value < 999_500:
        return f"{round(value / 1_000)}k"
    millions = value / 1_000_000
    if millions < 99.95:
        return f"{millions:.1f}M"
    return f"{round(millions)}M"


def cached_percent(totals: UsageTotals) -> int:
    if totals.input_tokens <= 0:
        return 0
    return math.floor(100 * min(totals.cached_input_tokens, totals.input_tokens) / totals.input_tokens)


def format_task_tokens_line(totals: UsageTotals | None) -> str:
    """``Tokens: 8.1M in (98% cached) · 62k out``, or "" when nothing is known."""

    if totals is None or totals.empty:
        return ""
    return (
        f"Tokens: {format_tokens(totals.input_tokens)} in "
        f"({cached_percent(totals)}% cached) · {format_tokens(totals.output_tokens)} out"
    )


def board_usage_line(report: UsageReport | None) -> str:
    """One stable board line, e.g. ``Today: 41M in · 0.4M out (tasks 78%, chat 15%, other 7%)``.

    Input is floored to whole millions, output to tenths of a million, and
    shares are whole percentages of input, so the board digest changes only
    when a bucket boundary is crossed, not on every chat message.
    """

    try:
        if report is None or report.total.empty:
            return ""
        total_input = report.total.input_tokens
        millions_in = total_input // 1_000_000
        input_text = f"{millions_in}M" if millions_in else "<1M"
        tenths_out = report.total.output_tokens // 100_000
        output_text = f"{tenths_out / 10:.1f}M" if tenths_out else "<0.1M"
        shares = []
        if total_input > 0:
            for label, categories in BOARD_GROUPS:
                group_input = sum(
                    report.by_category[category].input_tokens
                    for category in categories
                    if category in report.by_category
                )
                percent = round(100 * group_input / total_input)
                if percent:
                    shares.append(f"{label} {percent}%")
        line = f"Today: {input_text} in · {output_text} out"
        return f"{line} ({', '.join(shares)})" if shares else line
    except Exception:
        return ""


def format_usage_report(
    report: UsageReport,
    *,
    label: str,
    task_titles: Mapping[int, str] | None = None,
    top_tasks: int = TOP_TASKS,
) -> str:
    """Read-only ``/usage`` report text."""

    titles = task_titles or {}
    period = (
        report.end_day
        if report.start_day == report.end_day
        else f"{report.start_day} to {report.end_day}"
    )
    lines = [f"Token usage, {label} ({period})"]
    if report.total.calls == 0:
        lines.append("No runtime calls recorded.")
        return "\n".join(lines)
    lines.append(f"Total: {_totals_text(report.total)}")
    lines.extend(["", "By category:"])
    total_input = report.total.input_tokens
    for category in CATEGORIES:
        totals = report.by_category.get(category)
        if totals is None or totals.calls == 0:
            continue
        share = f", {round(100 * totals.input_tokens / total_input)}% of input" if total_input else ""
        lines.append(f"- {category}: {_totals_text(totals)}{share}")
    ranked = sorted(
        report.by_task.items(),
        key=lambda item: (-item[1].input_tokens, item[0]),
    )[: max(0, top_tasks)]
    if ranked:
        lines.extend(["", "Top tasks by input:"])
        for task_id, totals in ranked:
            title = titles.get(task_id, "")
            name = f"#{task_id} {title}".rstrip()
            lines.append(
                f"- {name}: {format_tokens(totals.input_tokens)} in · "
                f"{format_tokens(totals.output_tokens)} out"
            )
    conversation = report.by_category.get(CATEGORY_CONVERSATION)
    lines.append("")
    if conversation is not None and conversation.calls:
        lines.append(
            f"Conversation turns: {conversation.calls} · average "
            f"{format_tokens(conversation.input_tokens // conversation.calls)} in "
            f"({cached_percent(conversation)}% cached) · "
            f"{format_tokens(conversation.output_tokens // conversation.calls)} out"
        )
    else:
        lines.append("Conversation turns: none")
    return "\n".join(lines)


def token_usage_help(prefix: str = "/") -> str:
    return "\n".join([
        f"{prefix}usage [today|7d]",
        "Show runtime token usage recorded by this Enoch: totals per category",
        "(task, ci-fix, conversation, snapshot, evolve, other), the top tasks by",
        "input tokens, and the average conversation turn. Defaults to today.",
        "Read-only: it reads the private usage ledger and makes no model or forge calls.",
        f"Use {prefix}quota for account limits and reset times.",
    ])


def parse_usage_period(argument: str) -> tuple[int, str] | None:
    """``(days, label)`` for a ``/usage`` argument, or None when it is invalid."""

    selected = " ".join(str(argument or "").lower().split())
    if selected in {"", "today", "1d", "day"}:
        return 1, "today"
    if selected in {"7d", "7", "week", "7 days"}:
        return 7, "last 7 days"
    return None


def _totals_text(totals: UsageTotals) -> str:
    calls = f"{totals.calls} call{'s' if totals.calls != 1 else ''}"
    return (
        f"{format_tokens(totals.input_tokens)} in ({cached_percent(totals)}% cached) · "
        f"{format_tokens(totals.output_tokens)} out · {calls}"
    )


# Internals ------------------------------------------------------------------


def _local(now: datetime | None) -> datetime:
    # Naive datetimes are taken as local time; the day key is the local date.
    return (now or datetime.now()).astimezone()


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def _usage_count(usage: object, name: str) -> int:
    if usage is None:
        return 0
    if isinstance(usage, Mapping):
        return _count(usage.get(name))
    return _count(getattr(usage, name, 0))


def _task_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _label(value: object) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned[:LABEL_LIMIT]


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _since_day(value: str | date | datetime | None) -> date | None:
    """Local day of ``value`` minus one day of slack for clock/zone edges."""

    try:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, date):
            return value - timedelta(days=1)
        else:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return _local(moment).date() - timedelta(days=1)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _file_day(path: Path) -> date | None:
    if path.suffix != ".jsonl":
        return None
    return _parse_day(path.stem)


def _stat_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (path.name, -1, -1)
    return (path.name, stat.st_size, stat.st_mtime_ns)


def _iter_records(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            continue


def _totals_from_record(record: Mapping[str, Any]) -> UsageTotals:
    totals = UsageTotals()
    totals.add(record, calls=1)
    return totals


def _day_category_totals(path: Path) -> dict[str, UsageTotals]:
    totals: dict[str, UsageTotals] = {}
    for record in _iter_records((path,)):
        category = normalize_category(str(record.get("category") or ""))
        totals.setdefault(category, UsageTotals()).add(_totals_from_record(record))
    return totals


def _load_summary(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    days = data.get("days") if isinstance(data, dict) else None
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "days": {
            str(day): dict(categories)
            for day, categories in (days or {}).items()
            if isinstance(categories, dict)
        },
    }
