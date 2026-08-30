from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Mapping, Protocol

from identity_benchmark.contracts import (
    ExpectationResult,
    IdentityStatement,
    JsonValue,
    Probe,
)
from identity_benchmark.scoring import (
    DeterministicScorer,
    ExpectationScorer,
    weighted_expectation_score,
)
from identity_benchmark.processes import run_text_command


EVALUATOR_PROTOCOL_VERSION = 1


class EvaluatorError(RuntimeError):
    """Raised when an evaluator cannot return a valid probe judgment."""


@dataclass(frozen=True)
class EvaluationResult:
    score: float
    expectation_results: tuple[ExpectationResult, ...] = ()
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
            or not 0.0 <= self.score <= 1.0
        ):
            raise EvaluatorError("evaluation score must be between 0 and 1")
        for key, value in self.metadata.items():
            if not isinstance(key, str):
                raise EvaluatorError("evaluation metadata keys must be text")
            _json_value(value, f"metadata.{key}")


@dataclass(frozen=True)
class EvaluationRequest:
    """Blinded evaluator input; excludes profile description and private provenance."""

    profile_id: str
    statements: tuple[IdentityStatement, ...]
    probe: Probe
    agent_response: str


class Evaluator(Protocol):
    """Evaluate one complete probe response using any scoring harness."""

    @property
    def evaluator_id(self) -> str: ...

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult: ...


@dataclass(frozen=True)
class DeterministicEvaluator:
    """Adapt the v1 expectation scorer to the probe-level evaluator API."""

    scorer: ExpectationScorer = field(default_factory=DeterministicScorer)
    evaluator_id: str = "deterministic-v1"

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        results = tuple(
            self.scorer.score(request.agent_response, expectation)
            for expectation in request.probe.expectations
        )
        return EvaluationResult(
            score=weighted_expectation_score(results),
            expectation_results=results,
            metadata={
                "evaluator_id": self.evaluator_id,
                "harness": "identity-benchmark-python",
                "rubric_version": "deterministic-expectations-v1",
            },
        )


@dataclass(frozen=True)
class CommandEvaluator:
    """Invoke a fresh JSON-over-stdin evaluator command for every probe."""

    command: tuple[str, ...]
    evaluator_id: str
    timeout_seconds: float = 120.0
    environment: Mapping[str, str] | None = None
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise EvaluatorError("evaluator command must not be empty")
        if not self.evaluator_id.strip():
            raise EvaluatorError("evaluator id must not be empty")
        if self.timeout_seconds <= 0:
            raise EvaluatorError("evaluator timeout must be positive")

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        payload = {
            "protocol_version": EVALUATOR_PROTOCOL_VERSION,
            "profile_id": request.profile_id,
            "statements": [statement.to_dict() for statement in request.statements],
            "probe": request.probe.to_dict(),
            "agent_response": request.agent_response,
        }
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
            raise EvaluatorError(
                "evaluator command timed out after "
                f"{self.timeout_seconds:g} seconds"
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise EvaluatorError(f"evaluator command failed: {error}") from error
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "no diagnostic output"
            )
            raise EvaluatorError(
                f"evaluator command exited with {completed.returncode}: {_clip(detail)}"
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise EvaluatorError(
                f"evaluator returned invalid JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise EvaluatorError("evaluator response must be an object")
        if value.get("protocol_version") != EVALUATOR_PROTOCOL_VERSION:
            raise EvaluatorError(
                f"evaluator protocol_version must be {EVALUATOR_PROTOCOL_VERSION}"
            )
        score = value.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise EvaluatorError("evaluator response.score must be a number")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise EvaluatorError("evaluator response.metadata must be an object")
        reported_id = metadata.get("evaluator_id")
        if reported_id is not None and (
            not isinstance(reported_id, str)
            or reported_id not in {"", self.evaluator_id}
        ):
            raise EvaluatorError(
                "evaluator response evaluator_id does not match configured evaluator"
            )
        materialized_metadata = {
            str(key): _json_value(item, f"metadata.{key}")
            for key, item in metadata.items()
        }
        materialized_metadata["evaluator_id"] = self.evaluator_id
        return EvaluationResult(
            score=float(score),
            metadata=materialized_metadata,
        )


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{label}[]") for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(item, f"{label}.{key}")
            for key, item in value.items()
        }
    raise EvaluatorError(f"{label} is not JSON-compatible")


def _clip(value: str, limit: int = 1000) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "…"
