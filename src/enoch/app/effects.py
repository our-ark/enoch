from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import threading
from typing import Callable, Iterator, TypeVar

from enoch.app.epoch import (
    DaemonEpoch,
    StaleDaemonEpoch,
    daemon_epoch_guard,
    require_current_daemon_epoch,
)
from enoch.providers.contracts import RuntimeExecutionControl
from enoch.providers.authorization import CapabilityAuthorizer


Result = TypeVar("Result")


class DaemonEffectFence:
    """Authorize external effects against one active daemon epoch."""

    def __init__(
        self,
        root: Path,
        epoch: DaemonEpoch,
        *,
        authorizer: CapabilityAuthorizer | None = None,
        monitor_interval_seconds: float = 0.1,
    ) -> None:
        self.root = root
        self.epoch = epoch
        self.authorizer = authorizer
        self.monitor_interval_seconds = max(0.01, monitor_interval_seconds)

    def require_current(self) -> None:
        require_current_daemon_epoch(self.epoch, self.root)

    def run(self, effect: Callable[..., Result], *args, **kwargs) -> Result:
        """Serialize a bounded effect with daemon takeover."""

        with daemon_epoch_guard(self.epoch, self.root):
            return effect(*args, **kwargs)

    def run_authorized(
        self,
        action: str,
        requirements: tuple[str, ...],
        effect: Callable[..., Result],
        *args,
        task_id: int | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs,
    ) -> Result:
        self.authorize(
            action,
            requirements,
            task_id=task_id,
            metadata=metadata,
        )
        return self.run(effect, *args, **kwargs)

    def run_runtime(
        self,
        effect: Callable[[RuntimeExecutionControl], Result],
        execution: RuntimeExecutionControl,
    ) -> Result:
        """Cancel a long runtime invocation when this daemon loses ownership."""

        with self.runtime_control(execution) as fenced_execution:
            return effect(fenced_execution)

    def run_runtime_authorized(
        self,
        action: str,
        requirements: tuple[str, ...],
        effect: Callable[[RuntimeExecutionControl], Result],
        execution: RuntimeExecutionControl,
        *,
        task_id: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Result:
        self.authorize(
            action,
            requirements,
            task_id=task_id,
            metadata=metadata,
        )
        return self.run_runtime(effect, execution)

    def authorize(
        self,
        action: str,
        requirements: tuple[str, ...],
        *,
        task_id: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.require_current()
        if self.authorizer is not None:
            self.authorizer.require(
                action,
                requirements,
                task_id=task_id,
                metadata=metadata,
            )

    @contextmanager
    def runtime_control(
        self,
        execution: RuntimeExecutionControl,
    ) -> Iterator[RuntimeExecutionControl]:
        self.require_current()
        cancellation = execution.cancellation_event or threading.Event()
        fenced_execution = replace(execution, cancellation_event=cancellation)
        stopped = threading.Event()
        monitor = threading.Thread(
            target=self._monitor_epoch,
            args=(cancellation, stopped),
            name=f"enoch-epoch-{self.epoch.generation}",
            daemon=True,
        )
        monitor.start()
        try:
            yield fenced_execution
        finally:
            stopped.set()
            monitor.join(timeout=max(1.0, self.monitor_interval_seconds * 2))
            self.require_current()

    def _monitor_epoch(
        self,
        cancellation: threading.Event,
        stopped: threading.Event,
    ) -> None:
        while not stopped.is_set():
            try:
                self.require_current()
            except StaleDaemonEpoch:
                cancellation.set()
                return
            stopped.wait(self.monitor_interval_seconds)
