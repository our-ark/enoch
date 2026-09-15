"""Account quota presentation for runtimes with an optional ``quota`` method.

Providers return None when their CLI is absent, or a mapping containing
``windows`` (label, used_percent, resets_at), optional plan/source, and an
optional safe, user-facing error. Missing quota data is never treated as zero.
This is an additive capability; the required AgentRuntime contract is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import re
from typing import Mapping

from enoch.providers.registry import available_providers, load_provider


def quota_usage(prefix: str = "/") -> str:
    return "\n".join([
        f"{prefix}quota [gpt|codex|claude|all]",
        "Show account quota remaining, usage windows, and reset times.",
        "With no argument, query installed runtime providers and skip missing CLIs.",
        "GPT is an alias for Codex. These are account limits, not task token counts.",
        "Automatic warnings check only the active runtime every minute, at 10%, 5%, and 1% remaining.",
        f"Use {prefix}quota all to manually check all installed runtime providers.",
    ])


def quota_command(argument: str, root: Path, *, runtime=None, prefix: str = "/") -> str:
    selected = argument.strip().lower()
    if selected not in {"", "all", "gpt", "codex", "claude"}:
        return quota_usage(prefix)
    selected = "codex" if selected == "gpt" else selected
    reports = [format_quota(quota_provider_label(name), snapshot)
               for name, snapshot in quota_snapshots(root, runtime=runtime, selected=selected)]
    if not reports:
        return "No quota-capable runtime CLI is available on this host."
    return "Account quota (shared across the account, not task token counts):\n\n" + "\n\n".join(reports)


def quota_provider_label(name: str) -> str:
    return "GPT / Codex" if name == "codex" else _label(name.title())


def quota_snapshots(root: Path, *, runtime=None, selected: str = ""):
    """Yield independent provider snapshots for manual queries and monitoring."""
    installed = set(available_providers("runtime", root))
    active_name = getattr(runtime, "name", "")
    active_reader = getattr(runtime, "quota", None)
    if callable(active_reader):
        installed.add(active_name)
    names = sorted(installed) if selected in {"", "all"} else [selected]
    for name in names:
        if name not in installed:
            continue
        try:
            provider = runtime if name == active_name and callable(active_reader) else load_provider("runtime", root, name=name)
            reader = getattr(provider, "quota", None)
            if not callable(reader):
                continue
            snapshot = reader(root)
            if snapshot is None:  # The provider's executable is not installed.
                continue
            if not isinstance(snapshot, Mapping):
                raise ValueError("Invalid quota snapshot")
        except Exception:
            # Provider exceptions/CLI stderr can contain credentials or account IDs.
            snapshot = {"error": "quota query failed. Check CLI login and version on this host."}
        yield name, snapshot


def format_quota(label: str, snapshot: Mapping, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    plan = _label(snapshot.get("plan", ""))
    lines = [f"{label}" + (f" ({plan})" if plan else "")]
    if error := snapshot.get("error"):
        lines.append(_label(error, limit=300))
    windows = snapshot.get("windows", ())
    if not isinstance(windows, (list, tuple)):
        windows = ()
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        used = _number(window.get("used_percent"))
        usage = "usage unknown; remaining unknown" if used is None or used < 0 else (
            f"{max(0, min(100, 100 - used)):g}% remaining ({used:g}% used)"
        )
        reset = _timestamp(window.get("resets_at"))
        reset_text = "reset unknown / not reported"
        if reset is not None:
            seconds = (reset - now).total_seconds()
            suffix = f"in {_duration(seconds)}" if seconds > 0 else "reset time passed; query again to confirm"
            reset_text = f"resets {reset.astimezone():%Y-%m-%d %H:%M %Z} ({suffix})"
        lines.append(f"- {_label(window.get('label', 'Window'))}: {usage}; {reset_text}")
    if not windows and not snapshot.get("error"):
        lines.append("Quota unavailable for this account or authentication mode.")
    if source := snapshot.get("source"):
        lines.append(f"Source: {_label(source)}; checked {now.astimezone():%Y-%m-%d %H:%M:%S %Z}.")
    return "\n".join(lines)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _timestamp(value) -> datetime | None:
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else None
        if (number := _number(value)) is not None and number > 0:
            return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _duration(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return " ".join(value for value in (
        f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m" if minutes else "",
    ) if value)


def _label(value, *, limit: int = 100) -> str:
    # Avoid remote bucket labels becoming chat mentions, links, or extra lines.
    return re.sub(r"[<>*&`\x00-\x1f\x7f]", " ", str(value or "")).strip()[:limit]
