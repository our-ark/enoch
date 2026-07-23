"""Runtime result fallback for installations pinned to provider-kit 0.1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RUNTIME_CONTRACT_VERSION = 1


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
