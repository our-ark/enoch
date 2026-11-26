"""Typed, bounded advice for recovering extension-owned tasks."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from queue import Empty, Queue
import threading
from typing import Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from enoch.tasks.queue import TaskJob, TaskTerminalEvidence

EXTENSION_RECOVERY_API_VERSION = 1
RECOVERY_HOLD_TRIGGER = "extension-recovery-hold"


@dataclass(frozen=True)
class ExtensionRecoverySnapshot:
    extension_name: str
    task: TaskJob


@dataclass(frozen=True)
class ExtensionRecoveryDecision:
    action: Literal["requeue", "hold", "finalize"]
    note: str = ""
    evidence: TaskTerminalEvidence | None = None

    def __post_init__(self) -> None:
        from enoch.tasks.queue import TaskTerminalEvidence

        if self.action not in {"requeue", "hold", "finalize"}:
            raise ValueError("Unknown extension recovery decision.")
        if not isinstance(self.note, str):
            raise ValueError("Recovery note must be text.")
        if self.action == "finalize":
            if not isinstance(self.evidence, TaskTerminalEvidence):
                raise ValueError("Finalization requires terminal evidence.")
        elif self.evidence is not None:
            raise ValueError("Only finalization accepts terminal evidence.")


ExtensionRecoveryHook = Callable[[ExtensionRecoverySnapshot], ExtensionRecoveryDecision]


def invoke_recovery_hook(
    hook: ExtensionRecoveryHook, snapshot: ExtensionRecoverySnapshot, timeout: float,
) -> ExtensionRecoveryDecision:
    # Timed-out trusted Python cannot be killed safely. The hook receives a
    # detached snapshot; its eventual decision is discarded after timeout.
    results: Queue[ExtensionRecoveryDecision] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            decision = hook(deepcopy(snapshot))
            if not isinstance(decision, ExtensionRecoveryDecision):
                raise ValueError("Recovery hook returned an invalid decision.")
        except BaseException as error:
            decision = ExtensionRecoveryDecision("hold", f"Recovery hook failed: {type(error).__name__}.")
        results.put(decision)

    threading.Thread(target=invoke, name="extension-recovery", daemon=True).start()
    try:
        return results.get(timeout=timeout)
    except Empty:
        return ExtensionRecoveryDecision("hold", "Recovery hook timed out; operator action required.")
