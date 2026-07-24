from __future__ import annotations

from pathlib import Path
from typing import Any

from enoch.formatting import summarize_for_log
from enoch.logs import log_system_event
from enoch.memory.store import ensure_long_term_memory


def record_direct_action(message: str, result: str, root: Path) -> None:
    try:
        log_system_event(
            "direct_action",
            root=root,
            details={
                "request": summarize_for_log(message),
                "result": summarize_for_log(result),
            },
        )
        ensure_long_term_memory(root)
    except OSError:
        return


def record_system_event(
    event: str,
    root: Path,
    *,
    status: str = "ok",
    details: dict[str, Any] | None = None,
) -> None:
    try:
        log_system_event(event, root=root, status=status, details=details)
    except OSError:
        return
