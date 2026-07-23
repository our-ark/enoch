"""Runtime contract fallback for installations pinned to provider-kit 0.1."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable

from our_ark_provider_kit import AgentRuntimeCancelled, AgentRuntimeError


RUNTIME_CONTRACT_VERSION = 1
RUNTIME_EXECUTION_CONTRACT_VERSION = 1


class AgentRuntimeTimedOut(AgentRuntimeError):
    """Raised when a runtime invocation exceeds its execution deadline."""


@dataclass(frozen=True)
class RuntimeProgress:
    elapsed_seconds: int
    stage: str = "running"
    message: str = ""
    sandbox: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = RUNTIME_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUNTIME_EXECUTION_CONTRACT_VERSION:
            raise ValueError(
                f"Runtime progress uses contract version {self.contract_version}; "
                f"supported version is {RUNTIME_EXECUTION_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "elapsed_seconds", max(0, int(self.elapsed_seconds)))
        object.__setattr__(self, "stage", str(self.stage).strip().lower() or "running")
        object.__setattr__(self, "message", str(self.message).strip())
        object.__setattr__(self, "sandbox", str(self.sandbox).strip())
        object.__setattr__(self, "session_id", str(self.session_id).strip())


RuntimeProgressCallback = Callable[[RuntimeProgress], None]


@dataclass(frozen=True)
class RuntimeExecutionControl:
    request_id: str = ""
    session_key: str = ""
    timeout_seconds: int | None = None
    cancellation_event: threading.Event | None = None
    timeout_event: threading.Event | None = None
    progress_callback: RuntimeProgressCallback | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    started_at_monotonic: float = field(default_factory=time.monotonic)
    contract_version: int = RUNTIME_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUNTIME_EXECUTION_CONTRACT_VERSION:
            raise ValueError(
                f"Runtime execution uses contract version {self.contract_version}; "
                f"supported version is {RUNTIME_EXECUTION_CONTRACT_VERSION}."
            )
        timeout = self.timeout_seconds
        if timeout is not None:
            timeout = int(timeout)
            if timeout <= 0:
                raise ValueError("Runtime execution timeout must be positive.")
        object.__setattr__(self, "request_id", str(self.request_id).strip())
        object.__setattr__(self, "session_key", str(self.session_key).strip())
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self,
            "started_at_monotonic",
            float(self.started_at_monotonic),
        )

    @property
    def timed_out(self) -> bool:
        if self.timeout_event is not None and self.timeout_event.is_set():
            return True
        return (
            self.timeout_seconds is not None
            and time.monotonic() - self.started_at_monotonic >= self.timeout_seconds
        )

    @property
    def cancelled(self) -> bool:
        return (
            not self.timed_out
            and self.cancellation_event is not None
            and self.cancellation_event.is_set()
        )

    def raise_if_stopped(self) -> None:
        if self.timed_out:
            raise AgentRuntimeTimedOut("Agent runtime execution timed out.")
        if self.cancelled:
            raise AgentRuntimeCancelled("Agent runtime execution was cancelled.")

    def emit_progress(self, progress: RuntimeProgress) -> None:
        if self.progress_callback is not None:
            self.progress_callback(progress)


@dataclass(frozen=True)
class RuntimeUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeOutputReference:
    kind: str
    uri: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeSideEffect:
    kind: str
    reference: str
    status: str = "completed"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeResult:
    final_text: str
    session_id: str = ""
    completion_reason: str = "completed"
    usage: RuntimeUsage = field(default_factory=RuntimeUsage)
    events: tuple[RuntimeEvent, ...] = ()
    output_refs: tuple[RuntimeOutputReference, ...] = ()
    side_effects: tuple[RuntimeSideEffect, ...] = ()
    contract_version: int = RUNTIME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUNTIME_CONTRACT_VERSION:
            raise ValueError(
                f"Runtime result uses contract version {self.contract_version}; "
                f"supported version is {RUNTIME_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "final_text", str(self.final_text))
        object.__setattr__(self, "session_id", str(self.session_id).strip())
        reason = str(self.completion_reason).strip().lower().replace("_", "-")
        object.__setattr__(self, "completion_reason", reason or "completed")


RuntimeResultLike = RuntimeResult | str


def normalize_runtime_result(value: RuntimeResultLike) -> RuntimeResult:
    if isinstance(value, RuntimeResult):
        return value
    if isinstance(value, str):
        return RuntimeResult(final_text=value)
    raise TypeError(
        "Agent runtime must return RuntimeResult or str, "
        f"not {type(value).__name__}."
    )
