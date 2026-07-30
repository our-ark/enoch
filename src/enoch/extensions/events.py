from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import cast

from enoch.extensions.contracts import (
    ExtensionTaskEvent,
    ExtensionTaskEventType,
)
from enoch.storage import StorageLayout
from enoch.tasks.events import TaskEvent, load_task_events


DELIVERY_SCHEMA_VERSION = 1
DELIVERED_TASK_EVENTS = {
    "queued",
    "started",
    "completed",
    "failed",
    "cancelled",
}
_DELIVERY_THREAD_LOCK = threading.RLock()


def undelivered_extension_task_events(
    root: Path,
    storage: StorageLayout,
    extension_name: str,
) -> tuple[ExtensionTaskEvent, ...]:
    delivered = load_extension_task_event_receipts(storage)
    events = []
    for event in load_task_events(root, limit=1_000_000):
        converted = _extension_event(extension_name, event)
        if converted is not None and converted.id not in delivered:
            events.append(converted)
    return tuple(events)


def acknowledge_extension_task_event(
    storage: StorageLayout,
    event: ExtensionTaskEvent,
) -> None:
    path = extension_task_event_receipt_path(storage)
    with _DELIVERY_THREAD_LOCK:
        if event.id in load_extension_task_event_receipts(storage):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema_version": DELIVERY_SCHEMA_VERSION,
                        "event_id": event.id,
                        "delivery_key": event.delivery_key,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())


def load_extension_task_event_receipts(
    storage: StorageLayout,
) -> frozenset[str]:
    path = extension_task_event_receipt_path(storage)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    event_ids = set()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == DELIVERY_SCHEMA_VERSION
        ):
            event_id = str(value.get("event_id") or "").strip()
            if event_id:
                event_ids.add(event_id)
    return frozenset(event_ids)


def extension_task_event_receipt_path(storage: StorageLayout) -> Path:
    return storage.private_path("task_event_receipts.jsonl")


def _extension_event(
    extension_name: str,
    event: TaskEvent,
) -> ExtensionTaskEvent | None:
    if (
        event.event not in DELIVERED_TASK_EVENTS
        or event.context_source != f"extension:{extension_name}"
    ):
        return None
    return ExtensionTaskEvent(
        id=event.id,
        extension_name=extension_name,
        task_id=event.task_id,
        event=cast(ExtensionTaskEventType, event.event),
        occurred_at=event.occurred_at,
        request=event.request,
        result_summary=event.result_summary,
        workspace_id=event.workspace_id,
        review_id=event.review_id,
        review_urls=event.review_urls,
        changed_files=event.changed_files,
        publish_stage=event.publish_stage,
        revision_id=event.revision_id,
        attempt=event.attempt,
        max_attempts=event.max_attempts,
        failure_code=event.failure_code,
        failure_class=event.failure_class,
        retryable=event.retryable,
        runtime_provider=event.runtime_provider,
        runtime_session_id=event.runtime_session_id,
        runtime_completion_reason=event.runtime_completion_reason,
        runtime_usage=dict(event.runtime_usage),
        runtime_output_refs=event.runtime_output_refs,
        runtime_side_effects=event.runtime_side_effects,
    )
