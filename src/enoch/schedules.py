"""Shared occurrence arithmetic for Enoch's schedulers.

Both the cron scheduler and the extension schedule surface answer the same
question: given the target a schedule last aimed at, when is the next one? This
module owns that arithmetic and the daylight-saving policy behind it so the two
schedulers cannot drift apart.

Daily schedules follow local wall-clock time in one IANA zone. Two local times
per year need an explicit rule:

* A fall-back repeat (the same wall-clock time twice) resolves to its first
  occurrence, meaning ``fold=0``.
* A spring-forward gap (a wall-clock time that never happens) is detected by a
  local -> UTC -> local round trip and resolves to the first instant that
  exists after the jump.

Occurrences are enumerated by the local calendar date they are *intended* for,
so every local date contributes exactly one intended occurrence and none is
skipped. The instant a gap occurrence actually executes is the first one after
the jump, which can belong to the following local date: where the gap crosses
local midnight -- ``23:30`` in ``America/Nuuk``, for one -- the previous date's
occurrence and the next date's own occurrence both execute on that next local
date. Callers that need a per-execution-day guarantee cannot take it from the
intended-date enumeration alone.

This module only calculates targets; it never rewrites one a caller already
persisted. A scheduler holding a target calculated under an earlier policy keeps
it until that occurrence is acknowledged, and the rules above govern every
target calculated after that.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DAILY_TIME_PATTERN = re.compile(r"(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)")
MAX_TIMEZONE_CHARS = 128


class ScheduleError(ValueError):
    """A schedule declaration could not be interpreted."""


def normalize_daily_time(value: object, *, label: str = "Schedule") -> str:
    """Return a validated ``HH:MM`` local time, or "" when unset."""

    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ScheduleError(f"{label} daily time must be a string.")
    daily_time = value.strip()
    if not DAILY_TIME_PATTERN.fullmatch(daily_time):
        raise ScheduleError(f"{label} daily time must look like HH:MM.")
    return daily_time


def normalize_timezone(value: object, *, label: str = "Schedule") -> str:
    """Return a bounded IANA zone name that this host can actually resolve."""

    if not isinstance(value, str):
        raise ScheduleError(f"{label} timezone must be a string.")
    name = value.strip()
    if not name or len(name) > MAX_TIMEZONE_CHARS:
        raise ScheduleError(
            f"{label} timezone must contain 1 to {MAX_TIMEZONE_CHARS} characters."
        )
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError(f"Unknown {label.lower()} timezone {value!r}.") from error
    return name


def next_daily_run(
    daily_time: str,
    timezone_name: str,
    current: datetime,
    *,
    label: str = "Schedule",
) -> datetime:
    """Return the next UTC instant matching one local wall-clock time.

    Candidates are the requested wall-clock time on today's and tomorrow's local
    calendar date, so each local date is intended exactly once and none is
    skipped. A candidate inside a spring-forward gap executes after the jump
    instead, so its execution can fall on the next local date and share it with
    that date's own occurrence.
    """

    time_of_day = _time_of_day(daily_time, label=label)
    zone = ZoneInfo(normalize_timezone(timezone_name, label=label))
    moment = _coerce_utc(current)
    target_date = moment.astimezone(zone).date()
    candidate = local_daily_instant(target_date, time_of_day, zone)
    if candidate <= moment:
        candidate = local_daily_instant(
            target_date + timedelta(days=1),
            time_of_day,
            zone,
        )
    return candidate


def next_interval_run(
    scheduled_for: datetime | None,
    interval_seconds: int,
    current: datetime,
) -> datetime:
    """Return the first anchored interval target strictly after ``current``.

    Interval schedules are fixed-rate rather than fixed-delay: targets stay
    anchored to ``scheduled_for`` and missed slots are skipped rather than
    replayed. Without an anchor the interval restarts from ``current``.
    """

    if interval_seconds <= 0:
        raise ScheduleError("Schedule interval must be greater than zero.")
    moment = _coerce_utc(current)
    if scheduled_for is None:
        return moment + timedelta(seconds=interval_seconds)
    candidate = _coerce_utc(scheduled_for) + timedelta(seconds=interval_seconds)
    if candidate > moment:
        return candidate
    missed = int((moment - candidate).total_seconds() // interval_seconds) + 1
    return candidate + timedelta(seconds=missed * interval_seconds)


def local_daily_instant(
    target_date: date,
    time_of_day: time,
    zone: ZoneInfo,
) -> datetime:
    """Resolve one local wall-clock time on one local date to a UTC instant.

    Ambiguous fall-back times resolve to their first occurrence. Times skipped
    by a spring-forward jump do not round trip through UTC, and resolve to the
    instant the jump landed on -- which is the next local date when the gap
    crosses local midnight.
    """

    wall_clock = datetime.combine(target_date, time_of_day)
    instant = wall_clock.replace(tzinfo=zone).astimezone(timezone.utc)
    instant = instant.replace(microsecond=0)
    if instant.astimezone(zone).replace(tzinfo=None) == wall_clock:
        return instant
    before_jump = wall_clock.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    return _offset_change_instant(before_jump, instant, zone)


def _offset_change_instant(
    before: datetime,
    after: datetime,
    zone: ZoneInfo,
) -> datetime:
    """Return the first instant in ``(before, after]`` that uses a new offset.

    ``before`` precedes an offset change that ``after`` follows, which is the
    shape a skipped wall-clock time leaves behind: reading it with the offset in
    force before the jump lands after it, and with the offset after the jump
    lands before it.
    """

    offset = before.astimezone(zone).utcoffset()
    while after - before > timedelta(seconds=1):
        middle = before + (after - before) / 2
        if middle.astimezone(zone).utcoffset() == offset:
            before = middle
        else:
            after = middle
    return after.replace(microsecond=0)


def _time_of_day(daily_time: str, *, label: str) -> time:
    normalized = normalize_daily_time(daily_time, label=label)
    if not normalized:
        raise ScheduleError(f"{label} daily time is required.")
    hour, minute = (int(part) for part in normalized.split(":"))
    return time(hour, minute)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)
