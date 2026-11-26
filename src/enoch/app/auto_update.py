"""Automatic ``/update`` after a merge the human asked for.

When Enoch lands a review into the authoritative branch of its own repository
because the human asked for it (``/pr merge``, a natural-language merge, the
pending-merge watcher, or the PR auto-merge policy), an update is marked
pending here, in private state, so it survives restarts.

The daemon loop runs the same flow as ``/update`` once no task is running:
update from the authoritative repository, run doctor, and restart only if
doctor passes, through the shared ``RestartCoordinator``. Merges that land
before the update runs share it, so several merges in a row produce one
update. The flag is cleared when the update completes, whatever the outcome;
a doctor failure sends one alert and keeps the current code running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
from typing import Sequence

from enoch.paths import private_state_path
from enoch.state import atomic_write, load_json_object


SCHEMA_VERSION = 1
AUTO_UPDATE_STATE_FILE = "auto_update.json"
_LOCK = threading.Lock()


@dataclass(frozen=True)
class LandedReview:
    """A review Enoch just landed, as far as auto-update needs to know it."""

    label: str
    url: str = ""
    repository: str = ""
    base_branch: str = ""


@dataclass(frozen=True)
class AutoUpdateState:
    # Labels (``#N``) of landed reviews waiting for the update; empty means none.
    pending: tuple[str, ...] = ()
    requested_at: str = ""
    # Revision and reviews of the last automatic restart, for the next startup message.
    restarted_into: str = ""
    restarted_for: tuple[str, ...] = field(default_factory=tuple)


def auto_update_path(root: Path | None = None) -> Path:
    return private_state_path(AUTO_UPDATE_STATE_FILE, root)


def load_auto_update_state(root: Path | None = None) -> AutoUpdateState:
    data = load_json_object(auto_update_path(root))
    return AutoUpdateState(
        pending=_labels(data.get("pending")),
        requested_at=str(data.get("requested_at") or ""),
        restarted_into=str(data.get("restarted_into") or ""),
        restarted_for=_labels(data.get("restarted_for")),
    )


def _save(state: AutoUpdateState, root: Path | None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pending": list(state.pending),
        "requested_at": state.requested_at,
        "restarted_into": state.restarted_into,
        "restarted_for": list(state.restarted_for),
    }
    atomic_write(
        auto_update_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def mark_update_pending(label: str, requested_at: str, root: Path | None = None) -> AutoUpdateState:
    """Add ``label`` to the pending update; merges before it runs share it."""

    with _LOCK:
        state = load_auto_update_state(root)
        pending = state.pending if label in state.pending else (*state.pending, label)
        state = AutoUpdateState(
            pending=pending,
            requested_at=state.requested_at or requested_at,
            restarted_into=state.restarted_into,
            restarted_for=state.restarted_for,
        )
        _save(state, root)
        return state


def clear_update_pending(
    labels: Sequence[str] | None = None,
    root: Path | None = None,
    *,
    restarted_into: str = "",
) -> AutoUpdateState:
    """Clear the reviews an update covered (all when ``labels`` is None).

    A merge that landed while the update ran stays pending for the next one.
    With ``restarted_into``, the startup message after the restart names that
    revision and the covered reviews.
    """

    with _LOCK:
        state = load_auto_update_state(root)
        covered = state.pending if labels is None else tuple(labels)
        remaining = tuple(label for label in state.pending if label not in covered)
        state = AutoUpdateState(
            pending=remaining,
            requested_at=state.requested_at if remaining else "",
            restarted_into=restarted_into or state.restarted_into,
            restarted_for=covered if restarted_into else state.restarted_for,
        )
        _save(state, root)
        return state


def take_restart_note(root: Path | None = None) -> str:
    """The startup line after an automatic restart, once; empty otherwise."""

    with _LOCK:
        state = load_auto_update_state(root)
        if not state.restarted_into:
            return ""
        _save(
            AutoUpdateState(pending=state.pending, requested_at=state.requested_at),
            root,
        )
    return auto_restart_note(state.restarted_into, state.restarted_for)


def auto_restart_note(revision: str, labels: Sequence[str]) -> str:
    merged = f" after merging {join_labels(labels)}" if labels else ""
    return f"Auto-updated to {revision.strip()[:7]}{merged}."


def doctor_failed_alert(
    labels: Sequence[str],
    checks: Sequence[str],
    running_revision: str = "",
) -> str:
    merged = f" after merging {join_labels(labels)}" if labels else ""
    failing = ", ".join(checks) or "doctor"
    running = f" ({running_revision.strip()[:7]})" if running_revision.strip() else ""
    return (
        f"⚠️ Auto-update{merged} stopped: doctor failed ({failing}). "
        f"Not restarting; still running the current code{running}."
    )


def update_failed_alert(labels: Sequence[str], reason: str) -> str:
    merged = f" after merging {join_labels(labels)}" if labels else ""
    detail = " ".join(str(reason or "").split())
    if len(detail) > 300:
        detail = detail[:297].rstrip() + "..."
    return (
        f"⚠️ Auto-update{merged} did not run: {detail or 'unknown error'} "
        "Still running the current code."
    )


def join_labels(labels: Sequence[str]) -> str:
    items = [str(label) for label in labels if str(label)]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())
