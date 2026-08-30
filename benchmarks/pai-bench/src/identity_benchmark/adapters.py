from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping, Protocol

from identity_benchmark.contracts import (
    BenchmarkProfileError,
    BenchmarkRequest,
    INSTANCE_PROTOCOL_VERSION,
    InstanceResponse,
    JsonValue,
    TransitionRequest,
    parse_instance_response,
)
from identity_benchmark.processes import run_text_command


class InstanceError(RuntimeError):
    """Raised when a benchmark instance cannot return a valid response."""


class AgentAdapter(Protocol):
    """Transport-neutral interface to one agent under evaluation."""

    @property
    def instance_id(self) -> str: ...

    def respond(self, request: BenchmarkRequest) -> InstanceResponse: ...


class TransitionAdapter(Protocol):
    """Optional control plane for applying state changes after inference."""

    def apply_transition(self, request: TransitionRequest) -> None: ...


# Backward-compatible name used by benchmark v1 callers.
InstanceAdapter = AgentAdapter


@dataclass(frozen=True)
class CommandInstance:
    """Invoke a fresh JSON-over-stdin command for every isolated probe."""

    command: tuple[str, ...]
    instance_id: str
    timeout_seconds: float = 120.0
    environment: Mapping[str, str] | None = None
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise InstanceError("Instance command must not be empty.")
        if not self.instance_id.strip():
            raise InstanceError("Instance id must not be empty.")
        if self.timeout_seconds <= 0:
            raise InstanceError("Instance timeout must be positive.")

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        completed = self._invoke(request.to_dict(), operation="response")
        try:
            value = json.loads(completed.stdout)
            return parse_instance_response(value)
        except (json.JSONDecodeError, BenchmarkProfileError) as error:
            raise InstanceError(
                f"Instance returned an invalid protocol response: {error}"
            ) from error

    def apply_transition(self, request: TransitionRequest) -> None:
        completed = self._invoke(
            request.to_dict(), operation="state transition"
        )
        try:
            value = json.loads(completed.stdout)
            _validate_transition_response(value)
        except (json.JSONDecodeError, BenchmarkProfileError) as error:
            raise InstanceError(
                f"Instance returned an invalid transition response: {error}"
            ) from error

    def _invoke(
        self, payload: dict[str, JsonValue], *, operation: str
    ) -> subprocess.CompletedProcess[str]:
        command_label = (
            "Instance command"
            if operation == "response"
            else f"Instance {operation} command"
        )
        try:
            completed = run_text_command(
                self.command,
                input_text=json.dumps(payload, ensure_ascii=False),
                timeout_seconds=self.timeout_seconds,
                environment=(
                    {**os.environ, **self.environment}
                    if self.environment is not None
                    else None
                ),
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired as error:
            raise InstanceError(
                f"{command_label} timed out after "
                f"{self.timeout_seconds:g} seconds."
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise InstanceError(
                f"{command_label} failed: {error}"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise InstanceError(
                f"{command_label} exited with "
                f"{completed.returncode}: {_clip(detail)}"
            )
        return completed


def _validate_transition_response(value: object) -> None:
    if not isinstance(value, dict):
        raise BenchmarkProfileError("transition response must be an object.")
    expected = {"protocol_version", "applied"}
    optional = {"metadata"}
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected - optional)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise BenchmarkProfileError(
            "transition response has invalid fields: " + "; ".join(details) + "."
        )
    if value["protocol_version"] != INSTANCE_PROTOCOL_VERSION:
        raise BenchmarkProfileError(
            "transition response protocol_version must be "
            f"{INSTANCE_PROTOCOL_VERSION}."
        )
    if value["applied"] is not True:
        raise BenchmarkProfileError("transition response.applied must be true.")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise BenchmarkProfileError(
            "transition response.metadata must be an object."
        )


def _clip(value: str, limit: int = 1000) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


# Public name that describes the role rather than its implementation history.
CommandAgentAdapter = CommandInstance
