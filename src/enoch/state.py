from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None


class StateError(RuntimeError):
    """Base error for durable Enoch state."""


class StateCorruptionError(StateError):
    """Raised when existing state cannot be decoded without losing data."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(
            f"Enoch state at {path} is unreadable ({detail}). "
            "The file was preserved; repair or move it before retrying."
        )
        self.path = path
        self.detail = detail


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def load_json_object(
    path: Path,
    *,
    default_factory: Callable[[], dict[str, Any]] = dict,
) -> dict[str, Any]:
    """Load a JSON object without converting damaged state into empty state."""

    if not path.exists():
        return default_factory()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise StateCorruptionError(path, str(error)) from error
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        detail = f"invalid JSON at line {error.lineno}, column {error.colno}"
        raise StateCorruptionError(path, detail) from error
    if not isinstance(value, dict):
        raise StateCorruptionError(path, "expected a JSON object")
    return value


def atomic_write(path: Path, text: str) -> None:
    """Durably replace a file using a unique sibling temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def file_transaction(path: Path) -> Iterator[None]:
    """Serialize read-modify-write operations across threads and processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    lock_path = path.with_suffix(path.suffix + ".lock")
    with thread_lock:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
