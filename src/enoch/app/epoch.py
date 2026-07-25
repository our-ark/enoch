from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from enoch.memory.paths import now as current_time
from enoch.paths import enoch_home
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object


SCHEMA_VERSION = 1


class StaleDaemonEpoch(RuntimeError):
    """Raised when an obsolete daemon attempts a fenced side effect."""


@dataclass(frozen=True)
class DaemonEpoch:
    token: str
    generation: int
    provider: str
    pid: int
    started_at: str


def daemon_epoch_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "daemon_epoch.json"


def begin_daemon_epoch(
    root: Path | None = None,
    *,
    provider: str = "chat",
) -> DaemonEpoch:
    path = daemon_epoch_path(root)
    with file_transaction(path):
        data = _load_epoch_data(path)
        current = _parse_epoch(data.get("current"))
        epoch = DaemonEpoch(
            token=uuid4().hex,
            generation=(current.generation if current is not None else 0) + 1,
            provider=provider.strip().lower() or "chat",
            pid=os.getpid(),
            started_at=current_time(),
        )
        _write_epoch(path, epoch)
        return epoch


def current_daemon_epoch(root: Path | None = None) -> DaemonEpoch | None:
    path = daemon_epoch_path(root)
    with file_transaction(path):
        return _parse_epoch(_load_epoch_data(path).get("current"))


def require_current_daemon_epoch(
    expected: DaemonEpoch,
    root: Path | None = None,
) -> None:
    current = current_daemon_epoch(root)
    if current is None or current.token != expected.token:
        actual = current.generation if current is not None else "none"
        raise StaleDaemonEpoch(
            f"Daemon epoch {expected.generation} is stale; current epoch is {actual}."
        )


@contextmanager
def daemon_epoch_guard(
    expected: DaemonEpoch,
    root: Path | None = None,
) -> Iterator[None]:
    """Hold the epoch lock across one external side effect and its receipt."""

    path = daemon_epoch_path(root)
    with file_transaction(path):
        current = _parse_epoch(_load_epoch_data(path).get("current"))
        if current is None or current.token != expected.token:
            actual = current.generation if current is not None else "none"
            raise StaleDaemonEpoch(
                f"Daemon epoch {expected.generation} is stale; current epoch is {actual}."
            )
        yield


def _load_epoch_data(path: Path) -> dict:
    data = load_json_object(
        path,
        default_factory=lambda: {"schema_version": SCHEMA_VERSION, "current": None},
    )
    schema = data.get("schema_version", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise StateCorruptionError(path, f"unsupported schema version {schema}")
    current = data.get("current")
    if current is not None and _parse_epoch(current) is None:
        raise StateCorruptionError(path, "found an invalid daemon epoch")
    return {"schema_version": SCHEMA_VERSION, "current": current}


def _write_epoch(path: Path, epoch: DaemonEpoch) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "current": {
            "token": epoch.token,
            "generation": epoch.generation,
            "provider": epoch.provider,
            "pid": epoch.pid,
            "started_at": epoch.started_at,
        },
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _parse_epoch(raw: object) -> DaemonEpoch | None:
    if not isinstance(raw, dict):
        return None
    token = str(raw.get("token") or "").strip()
    provider = str(raw.get("provider") or "").strip()
    started_at = str(raw.get("started_at") or "").strip()
    generation = _positive_int(raw.get("generation"))
    pid = _positive_int(raw.get("pid"))
    if not token or not provider or not started_at or generation <= 0 or pid <= 0:
        return None
    return DaemonEpoch(
        token=token,
        generation=generation,
        provider=provider,
        pid=pid,
        started_at=started_at,
    )


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
