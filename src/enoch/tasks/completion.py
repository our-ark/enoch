"""Typed completion advice; only the workflow engine commits transitions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from queue import Empty, Queue
import threading
from typing import Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from enoch.tasks.queue import TaskJob

COMPLETION_GATE_API_VERSION = 1
TERMINAL_CHILD_STATES = frozenset({"completed", "failed", "cancelled", "regressed"})


def gate_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Completion deadlines require an explicit timezone.")
    return parsed


@dataclass(frozen=True)
class CompletionGate:
    deadline_at: str
    child_task_ids: tuple[int, ...] = ()
    evidence_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.deadline_at, str):
            raise ValueError("Completion deadline must be an ISO timestamp.")
        gate_time(self.deadline_at)
        if not isinstance(self.child_task_ids, tuple) or any(
            type(item) is not int or item <= 0 for item in self.child_task_ids
        ) or len(set(self.child_task_ids)) != len(self.child_task_ids):
            raise ValueError("Completion children must be unique positive task IDs.")
        if not isinstance(self.evidence_keys, tuple) or any(
            not isinstance(key, str) or not key.strip() or key != key.strip()
            for key in self.evidence_keys
        ) or len(set(self.evidence_keys)) != len(self.evidence_keys):
            raise ValueError("Completion evidence names must be unique nonempty strings.")
        if not self.child_task_ids and not self.evidence_keys:
            raise ValueError("A completion gate must name children or evidence.")


@dataclass(frozen=True)
class CompletionSnapshot:
    extension_name: str
    task: TaskJob
    children: tuple[TaskJob, ...]


@dataclass(frozen=True)
class CompletionDecision:
    action: Literal["complete", "extend", "fail"]
    note: str = ""
    gate: CompletionGate | None = None

    def __post_init__(self) -> None:
        if self.action not in {"complete", "extend", "fail"} or not isinstance(self.note, str):
            raise ValueError("Invalid completion decision.")
        if self.action == "extend":
            if not isinstance(self.gate, CompletionGate):
                raise ValueError("Extension requires a replacement completion gate.")
        elif self.gate is not None:
            raise ValueError("Only an extension decision accepts a gate.")


CompletionHook = Callable[[CompletionSnapshot], CompletionDecision]


def invoke_completion_hook(hook: CompletionHook, snapshot: CompletionSnapshot, timeout: float) -> CompletionDecision:
    results: Queue[CompletionDecision] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            decision = hook(deepcopy(snapshot))
            if not isinstance(decision, CompletionDecision):
                raise ValueError("Invalid completion hook result.")
        except BaseException as error:
            decision = CompletionDecision("fail", f"Completion hook failed: {type(error).__name__}.")
        results.put(decision)

    # Hooks are trusted read-only advice. Late results cannot mutate the queue.
    threading.Thread(target=invoke, name="extension-completion", daemon=True).start()
    try:
        return results.get(timeout=timeout)
    except Empty:
        return CompletionDecision("fail", "Completion hook timed out.")
