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
    InstanceResponse,
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
        try:
            completed = run_text_command(
                self.command,
                input_text=json.dumps(request.to_dict(), ensure_ascii=False),
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
                "Instance command timed out after "
                f"{self.timeout_seconds:g} seconds."
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise InstanceError(f"Instance command failed: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise InstanceError(
                f"Instance command exited with {completed.returncode}: {_clip(detail)}"
            )
        try:
            value = json.loads(completed.stdout)
            return parse_instance_response(value)
        except (json.JSONDecodeError, BenchmarkProfileError) as error:
            raise InstanceError(f"Instance returned an invalid protocol response: {error}") from error


def _clip(value: str, limit: int = 1000) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


# Public name that describes the role rather than its implementation history.
CommandAgentAdapter = CommandInstance
