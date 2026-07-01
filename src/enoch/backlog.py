from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 1
PRIORITIES = ("p0", "p1", "p2")
DEFAULT_PRIORITY = "p1"
_BACKLOG_THREAD_LOCK = threading.RLock()


@dataclass(frozen=True)
class BacklogItem:
    id: int
    chat_id: int
    text: str
    priority: str
    created_at: str
    promoted_at: str = ""
    completed_at: str = ""
    status: str = "pending"
    promoted_task_id: int | None = None
    context: str = ""
    context_source: str = ""


@dataclass(frozen=True)
class BacklogStatus:
    pending_count: int
    pending: tuple[BacklogItem, ...] = ()
    history: tuple[BacklogItem, ...] = ()


def backlog_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "backlog.json"


def add_backlog_item(
    chat_id: int,
    text: str,
    root: Path | None = None,
    *,
    priority: str = DEFAULT_PRIORITY,
    context: str = "",
    context_source: str = "",
) -> BacklogItem:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Backlog text is required.")
    priority = normalize_priority(priority)
    with _backlog_transaction(root):
        data = _load_backlog(root)
        item = BacklogItem(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            priority=priority,
            created_at=current_time(),
            context=context.strip(),
            context_source=context_source.strip(),
        )
        pending = data.setdefault("pending", [])
        pending.append(_item_to_dict(item))
        data["next_id"] = item.id + 1
        _write_backlog(data, root)
        return item


def remove_backlog_item(item_id: int, root: Path | None = None) -> BacklogItem | None:
    with _backlog_transaction(root):
        data = _load_backlog(root)
        kept: list[BacklogItem] = []
        removed: BacklogItem | None = None
        for item in _pending_items(data):
            if item.id == item_id and removed is None:
                removed = _replace_item(item, status="removed", completed_at=current_time())
            else:
                kept.append(item)
        if removed is None:
            return None
        history = _history_items(data)
        history.append(removed)
        data["pending"] = [_item_to_dict(item) for item in kept]
        data["history"] = [_item_to_dict(item) for item in history[-20:]]
        _write_backlog(data, root)
        return removed


def reprioritize_backlog_item(item_id: int, priority: str, root: Path | None = None) -> BacklogItem | None:
    priority = normalize_priority(priority)
    with _backlog_transaction(root):
        data = _load_backlog(root)
        pending = []
        changed: BacklogItem | None = None
        for item in _pending_items(data):
            if item.id == item_id and changed is None:
                item = _replace_item(item, priority=priority)
                changed = item
            pending.append(_item_to_dict(item))
        if changed is None:
            return None
        data["pending"] = pending
        _write_backlog(data, root)
        return changed


def promote_next_backlog_item(root: Path | None = None, *, promoted_task_id: int | None = None) -> BacklogItem | None:
    with _backlog_transaction(root):
        data = _load_backlog(root)
        pending = _pending_items(data)
        item_index = _next_item_index(pending)
        if item_index is None:
            return None
        item = pending.pop(item_index)
        promoted = _replace_item(
            item,
            status="promoted",
            promoted_at=current_time(),
            promoted_task_id=promoted_task_id,
        )
        history = _history_items(data)
        history.append(promoted)
        data["pending"] = [_item_to_dict(item) for item in pending]
        data["history"] = [_item_to_dict(item) for item in history[-20:]]
        _write_backlog(data, root)
        return promoted


def promote_backlog_item(
    item_id: int,
    root: Path | None = None,
    *,
    promoted_task_id: int | None = None,
) -> BacklogItem | None:
    with _backlog_transaction(root):
        data = _load_backlog(root)
        kept: list[BacklogItem] = []
        promoted: BacklogItem | None = None
        for item in _pending_items(data):
            if item.id == item_id and promoted is None:
                promoted = _replace_item(
                    item,
                    status="promoted",
                    promoted_at=current_time(),
                    promoted_task_id=promoted_task_id,
                )
            else:
                kept.append(item)
        if promoted is None:
            return None
        history = _history_items(data)
        history.append(promoted)
        data["pending"] = [_item_to_dict(item) for item in kept]
        data["history"] = [_item_to_dict(item) for item in history[-20:]]
        _write_backlog(data, root)
        return promoted


def next_backlog_item(root: Path | None = None) -> BacklogItem | None:
    with _backlog_transaction(root):
        pending = _pending_items(_load_backlog(root))
        item_index = _next_item_index(pending)
        if item_index is None:
            return None
        return pending[item_index]


def backlog_item(item_id: int, root: Path | None = None) -> BacklogItem | None:
    with _backlog_transaction(root):
        for item in _pending_items(_load_backlog(root)):
            if item.id == item_id:
                return item
    return None


def backlog_status(root: Path | None = None) -> BacklogStatus:
    with _backlog_transaction(root):
        data = _load_backlog(root)
        pending = tuple(_pending_items(data))
        return BacklogStatus(
            pending_count=len(pending),
            pending=pending,
            history=tuple(_history_items(data)),
        )


def normalize_priority(priority: str) -> str:
    normalized = priority.strip().lower()
    if normalized not in PRIORITIES:
        raise ValueError("Backlog priority must be p0, p1, or p2.")
    return normalized


def _next_item_index(items: list[BacklogItem]) -> int | None:
    for priority in PRIORITIES:
        for index, item in enumerate(items):
            if item.priority == priority:
                return index
    return None


def _load_backlog(root: Path | None = None) -> dict:
    path = backlog_path(root)
    if not path.exists():
        return _empty_backlog()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_backlog()
    if not isinstance(raw, dict):
        return _empty_backlog()
    pending = [_item_to_dict(item) for item in _pending_items(raw)]
    next_id = _int(raw.get("next_id"), default=1)
    max_id = max([item["id"] for item in pending], default=0)
    history_items = _history_items(raw)
    history = [_item_to_dict(item) for item in history_items]
    if history_items:
        max_id = max(max_id, max(item.id for item in history_items))
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": max(next_id, max_id + 1),
        "pending": pending,
        "history": history[-20:],
    }


def _write_backlog(data: dict, root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "next_id": _next_id(data),
        "pending": [_item_to_dict(item) for item in _pending_items(data)],
        "history": [_item_to_dict(item) for item in _history_items(data)][-20:],
    }
    atomic_write(backlog_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


@contextmanager
def _backlog_transaction(root: Path | None = None):
    path = backlog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _BACKLOG_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _pending_items(data: dict) -> list[BacklogItem]:
    raw = data.get("pending")
    if not isinstance(raw, list):
        return []
    items = [_parse_item(item) for item in raw]
    return [item for item in items if item is not None]


def _history_items(data: dict) -> list[BacklogItem]:
    raw = data.get("history")
    if not isinstance(raw, list):
        return []
    items = [_parse_item(item) for item in raw]
    return [item for item in items if item is not None]


def _parse_item(raw: object) -> BacklogItem | None:
    if not isinstance(raw, dict):
        return None
    item_id = _int(raw.get("id"))
    chat_id = _int(raw.get("chat_id"))
    text = str(raw.get("text") or "").strip()
    try:
        priority = normalize_priority(str(raw.get("priority") or DEFAULT_PRIORITY))
    except ValueError:
        priority = DEFAULT_PRIORITY
    created_at = str(raw.get("created_at") or "").strip()
    promoted_at = str(raw.get("promoted_at") or "").strip()
    completed_at = str(raw.get("completed_at") or "").strip()
    status = str(raw.get("status") or "").strip() or "pending"
    promoted_task_id = _optional_int(raw.get("promoted_task_id"))
    context = str(raw.get("context") or "").strip()
    context_source = str(raw.get("context_source") or "").strip()
    if item_id <= 0 or not isinstance(chat_id, int) or not text:
        return None
    return BacklogItem(
        id=item_id,
        chat_id=chat_id,
        text=text,
        priority=priority,
        created_at=created_at,
        promoted_at=promoted_at,
        completed_at=completed_at,
        status=status,
        promoted_task_id=promoted_task_id,
        context=context,
        context_source=context_source,
    )


def _item_to_dict(item: BacklogItem | None) -> dict:
    if item is None:
        return {}
    return {
        "id": item.id,
        "chat_id": item.chat_id,
        "text": item.text,
        "priority": item.priority,
        "created_at": item.created_at,
        "promoted_at": item.promoted_at,
        "completed_at": item.completed_at,
        "status": item.status,
        "promoted_task_id": item.promoted_task_id,
        "context": item.context,
        "context_source": item.context_source,
    }


def _replace_item(item: BacklogItem, **changes: object) -> BacklogItem:
    values = {
        "id": item.id,
        "chat_id": item.chat_id,
        "text": item.text,
        "priority": item.priority,
        "created_at": item.created_at,
        "promoted_at": item.promoted_at,
        "completed_at": item.completed_at,
        "status": item.status,
        "promoted_task_id": item.promoted_task_id,
        "context": item.context,
        "context_source": item.context_source,
    }
    values.update(changes)
    return BacklogItem(**values)


def _next_id(data: dict) -> int:
    return max(1, _int(data.get("next_id"), default=1))


def _int(value: object, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _optional_int(value: object) -> int | None:
    parsed = _int(value)
    return parsed if parsed > 0 else None


def _empty_backlog() -> dict:
    return {"schema_version": SCHEMA_VERSION, "next_id": 1, "pending": [], "history": []}
