from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, Literal, TypeAlias


SCHEMA_VERSION = 1
INSTANCE_PROTOCOL_VERSION = 1
Dimension = Literal[
    "recognition",
    "application",
    "consistency",
    "resistance",
    "separation",
    "retention",
    "governance",
    "capability",
]
ExpectationType = Literal["exact", "contains", "excludes", "regex"]
ExpectationAspect = Literal["identity", "format", "constraint"]
TransitionType = Literal["replace-agent-identity"]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

DIMENSIONS = {
    "recognition",
    "application",
    "consistency",
    "resistance",
    "separation",
    "retention",
    "governance",
    "capability",
}
EXPECTATION_TYPES = {"exact", "contains", "excludes", "regex"}
EXPECTATION_ASPECTS = {"identity", "format", "constraint"}
TRANSITION_TYPES = {"replace-agent-identity"}
MESSAGE_ROLES = {"system", "user", "assistant"}
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")


class BenchmarkProfileError(ValueError):
    """Raised when an identity benchmark profile is malformed."""


@dataclass(frozen=True)
class IdentityStatement:
    id: str
    content: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "content": self.content}


@dataclass(frozen=True)
class Message:
    role: str
    content: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Expectation:
    type: ExpectationType
    value: str
    weight: float = 1.0
    gate: bool = False
    aspect: ExpectationAspect = "identity"

    def to_dict(self) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "type": self.type,
            "value": self.value,
            "weight": self.weight,
            "gate": self.gate,
        }
        if self.aspect != "identity":
            value["aspect"] = self.aspect
        return value


@dataclass(frozen=True)
class StateTransition:
    type: TransitionType
    agent_identity: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "type": self.type,
            "agent_identity": self.agent_identity,
        }


@dataclass(frozen=True)
class Probe:
    id: str
    dimension: Dimension
    messages: tuple[Message, ...]
    expectations: tuple[Expectation, ...]
    weight: float = 1.0
    tags: tuple[str, ...] = ()
    after_response: StateTransition | None = None
    reference_statements: tuple[IdentityStatement, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "id": self.id,
            "dimension": self.dimension,
            "messages": [message.to_dict() for message in self.messages],
            "expectations": [expectation.to_dict() for expectation in self.expectations],
            "weight": self.weight,
            "tags": list(self.tags),
        }
        if self.after_response is not None:
            value["after_response"] = self.after_response.to_dict()
        if self.reference_statements:
            value["reference_statements"] = [
                statement.to_dict() for statement in self.reference_statements
            ]
        return value


@dataclass(frozen=True)
class BenchmarkProfile:
    profile_id: str
    statements: tuple[IdentityStatement, ...]
    probes: tuple[Probe, ...]
    agent_identity: dict[str, JsonValue] | None = None
    description: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "statements": [statement.to_dict() for statement in self.statements],
            "probes": [probe.to_dict() for probe in self.probes],
        }
        if self.description:
            value["description"] = self.description
        if self.agent_identity is not None:
            value["agent_identity"] = self.agent_identity
        return value


@dataclass(frozen=True)
class BenchmarkRequest:
    profile_id: str
    probe_id: str
    messages: tuple[Message, ...]
    after_response: StateTransition | None = None
    protocol_version: int = INSTANCE_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "protocol_version": self.protocol_version,
            "profile_id": self.profile_id,
            "probe_id": self.probe_id,
            "messages": [message.to_dict() for message in self.messages],
        }
        if self.after_response is not None:
            value["after_response"] = self.after_response.to_dict()
        return value


@dataclass(frozen=True)
class InstanceResponse:
    response: str
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    protocol_version: int = INSTANCE_PROTOCOL_VERSION


@dataclass(frozen=True)
class ExpectationResult:
    expectation: Expectation
    passed: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "expectation": self.expectation.to_dict(),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ProbeResult:
    probe_id: str
    dimension: str
    score: float
    weight: float
    response: str
    expectations: tuple[ExpectationResult, ...]
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    error: str = ""
    evaluation_metadata: dict[str, JsonValue] = field(default_factory=dict)
    component_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "probe_id": self.probe_id,
            "dimension": self.dimension,
            "score": self.score,
            "weight": self.weight,
            "response": self.response,
            "expectations": [result.to_dict() for result in self.expectations],
            "metadata": self.metadata,
            "evaluation_metadata": self.evaluation_metadata,
            "component_scores": self.component_scores,
            "error": self.error,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    profile_id: str
    instance_id: str
    started_at: str
    finished_at: str
    score: float
    dimension_scores: dict[str, float]
    metric_scores: dict[str, float]
    results: tuple[ProbeResult, ...]
    evaluator_id: str = "deterministic-v1"
    protocol_version: int = INSTANCE_PROTOCOL_VERSION
    benchmark_version: int = SCHEMA_VERSION

    @property
    def errors(self) -> int:
        return sum(bool(result.error) for result in self.results)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "benchmark_version": self.benchmark_version,
            "instance_protocol_version": self.protocol_version,
            "profile_id": self.profile_id,
            "instance_id": self.instance_id,
            "evaluator_id": self.evaluator_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "score": self.score,
            "dimension_scores": self.dimension_scores,
            "metric_scores": self.metric_scores,
            "errors": self.errors,
            "results": [result.to_dict() for result in self.results],
        }


def load_benchmark_profile(path: Path) -> BenchmarkProfile:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BenchmarkProfileError(f"Could not load benchmark profile: {error}") from error
    return parse_benchmark_profile(value)


def parse_benchmark_report(value: object) -> BenchmarkReport:
    """Reconstruct and validate a report previously emitted by the runner."""
    root = _mapping(value, "benchmark report")
    _keys(
        root,
        "benchmark report",
        required={
            "benchmark_version",
            "instance_protocol_version",
            "profile_id",
            "instance_id",
            "evaluator_id",
            "started_at",
            "finished_at",
            "score",
            "dimension_scores",
            "metric_scores",
            "errors",
            "results",
        },
    )
    if root["benchmark_version"] != SCHEMA_VERSION:
        raise BenchmarkProfileError(
            "benchmark report benchmark_version must be "
            f"{SCHEMA_VERSION}; received {root['benchmark_version']!r}."
        )
    if root["instance_protocol_version"] != INSTANCE_PROTOCOL_VERSION:
        raise BenchmarkProfileError(
            "benchmark report instance_protocol_version must be "
            f"{INSTANCE_PROTOCOL_VERSION}; received "
            f"{root['instance_protocol_version']!r}."
        )
    results = tuple(
        _probe_result(item, index)
        for index, item in enumerate(
            _nonempty_list(root["results"], "benchmark report.results")
        )
    )
    report = BenchmarkReport(
        profile_id=_identifier(root["profile_id"], "benchmark report.profile_id"),
        instance_id=_text(root["instance_id"], "benchmark report.instance_id"),
        evaluator_id=_identifier(
            root["evaluator_id"], "benchmark report.evaluator_id"
        ),
        started_at=_text(root["started_at"], "benchmark report.started_at"),
        finished_at=_text(root["finished_at"], "benchmark report.finished_at"),
        score=_unit_score(root["score"], "benchmark report.score"),
        dimension_scores=_score_mapping(
            root["dimension_scores"], "benchmark report.dimension_scores"
        ),
        metric_scores=_score_mapping(
            root["metric_scores"], "benchmark report.metric_scores"
        ),
        results=results,
    )
    errors = root["errors"]
    if isinstance(errors, bool) or not isinstance(errors, int) or errors < 0:
        raise BenchmarkProfileError(
            "benchmark report.errors must be a non-negative integer."
        )
    if errors != report.errors:
        raise BenchmarkProfileError(
            "benchmark report.errors does not match its probe results."
        )
    return report


def parse_benchmark_profile(value: object) -> BenchmarkProfile:
    root = _mapping(value, "benchmark profile")
    _keys(
        root,
        "benchmark profile",
        required={"schema_version", "profile_id", "statements", "probes"},
        optional={"$schema", "description", "agent_identity"},
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise BenchmarkProfileError(
            f"schema_version must be {SCHEMA_VERSION}; received {root['schema_version']!r}."
        )
    if "$schema" in root:
        _text(root["$schema"], "$schema")
    profile_id = _identifier(root["profile_id"], "profile_id")
    description = _optional_text(root.get("description"), "description")
    agent_identity = None
    if "agent_identity" in root:
        identity = _mapping(root["agent_identity"], "agent_identity")
        agent_identity = {
            str(key): _json_value(item, f"agent_identity.{key}")
            for key, item in identity.items()
        }
    statements = tuple(
        _statement(item, index) for index, item in enumerate(_nonempty_list(root["statements"], "statements"))
    )
    probes = tuple(
        _probe(item, index) for index, item in enumerate(_nonempty_list(root["probes"], "probes"))
    )
    _unique((statement.id for statement in statements), "statement ids")
    _unique((probe.id for probe in probes), "probe ids")
    return BenchmarkProfile(
        profile_id=profile_id,
        statements=statements,
        probes=probes,
        agent_identity=agent_identity,
        description=description,
    )


def parse_instance_response(value: object) -> InstanceResponse:
    root = _mapping(value, "instance response")
    _keys(
        root,
        "instance response",
        required={"protocol_version", "response"},
        optional={"metadata"},
    )
    if root["protocol_version"] != INSTANCE_PROTOCOL_VERSION:
        raise BenchmarkProfileError(
            "instance response protocol_version must be "
            f"{INSTANCE_PROTOCOL_VERSION}; received {root['protocol_version']!r}."
        )
    response = root["response"]
    if not isinstance(response, str):
        raise BenchmarkProfileError("instance response.response must be text.")
    metadata = root.get("metadata", {})
    if not isinstance(metadata, dict):
        raise BenchmarkProfileError("instance response.metadata must be an object.")
    return InstanceResponse(
        response=response,
        metadata={str(key): _json_value(item, f"metadata.{key}") for key, item in metadata.items()},
    )


def parse_benchmark_request(value: object) -> BenchmarkRequest:
    root = _mapping(value, "benchmark request")
    _keys(
        root,
        "benchmark request",
        required={"protocol_version", "profile_id", "probe_id", "messages"},
        optional={"after_response"},
    )
    if root["protocol_version"] != INSTANCE_PROTOCOL_VERSION:
        raise BenchmarkProfileError(
            "benchmark request protocol_version must be "
            f"{INSTANCE_PROTOCOL_VERSION}; received {root['protocol_version']!r}."
        )
    messages = tuple(
        _message(message, "benchmark request", index)
        for index, message in enumerate(
            _nonempty_list(root["messages"], "benchmark request.messages")
        )
    )
    if messages[-1].role != "user":
        raise BenchmarkProfileError(
            "benchmark request.messages must end with a user message."
        )
    return BenchmarkRequest(
        profile_id=_identifier(root["profile_id"], "benchmark request.profile_id"),
        probe_id=_identifier(root["probe_id"], "benchmark request.probe_id"),
        messages=messages,
        after_response=(
            _state_transition(root["after_response"], "benchmark request.after_response")
            if "after_response" in root
            else None
        ),
    )


def _statement(value: object, index: int) -> IdentityStatement:
    label = f"statements[{index}]"
    item = _mapping(value, label)
    _keys(item, label, required={"id", "content"})
    return IdentityStatement(
        id=_identifier(item["id"], f"{label}.id"),
        content=_text(item["content"], f"{label}.content"),
    )


def _probe(value: object, index: int) -> Probe:
    label = f"probes[{index}]"
    item = _mapping(value, label)
    _keys(
        item,
        label,
        required={"id", "dimension", "messages", "expectations"},
        optional={"weight", "tags", "after_response", "reference_statements"},
    )
    dimension = _text(item["dimension"], f"{label}.dimension")
    if dimension not in DIMENSIONS:
        raise BenchmarkProfileError(
            f"{label}.dimension must be one of: {', '.join(sorted(DIMENSIONS))}."
        )
    messages = tuple(
        _message(message, label, message_index)
        for message_index, message in enumerate(_nonempty_list(item["messages"], f"{label}.messages"))
    )
    if messages[-1].role != "user":
        raise BenchmarkProfileError(f"{label}.messages must end with a user message.")
    expectations = tuple(
        _expectation(expectation, label, expectation_index)
        for expectation_index, expectation in enumerate(
            _nonempty_list(item["expectations"], f"{label}.expectations")
        )
    )
    if not any(expectation.aspect == "identity" for expectation in expectations):
        raise BenchmarkProfileError(
            f"{label}.expectations must include at least one identity aspect."
        )
    tags_value = item.get("tags", [])
    if not isinstance(tags_value, list):
        raise BenchmarkProfileError(f"{label}.tags must be a list.")
    tags = tuple(_identifier(tag, f"{label}.tags") for tag in tags_value)
    _unique(iter(tags), f"{label}.tags")
    reference_statements = tuple(
        _statement(statement, statement_index)
        for statement_index, statement in enumerate(
            _nonempty_list(
                item["reference_statements"],
                f"{label}.reference_statements",
            )
        )
    ) if "reference_statements" in item else ()
    _unique(
        (statement.id for statement in reference_statements),
        f"{label}.reference_statements ids",
    )
    return Probe(
        id=_identifier(item["id"], f"{label}.id"),
        dimension=dimension,  # type: ignore[arg-type]
        messages=messages,
        expectations=expectations,
        weight=_positive_number(item.get("weight", 1.0), f"{label}.weight"),
        tags=tags,
        after_response=(
            _state_transition(item["after_response"], f"{label}.after_response")
            if "after_response" in item
            else None
        ),
        reference_statements=reference_statements,
    )


def _message(value: object, probe_label: str, index: int) -> Message:
    label = f"{probe_label}.messages[{index}]"
    item = _mapping(value, label)
    _keys(item, label, required={"role", "content"})
    role = _text(item["role"], f"{label}.role")
    if role not in MESSAGE_ROLES:
        raise BenchmarkProfileError(
            f"{label}.role must be one of: {', '.join(sorted(MESSAGE_ROLES))}."
        )
    return Message(role=role, content=_text(item["content"], f"{label}.content"))


def _expectation(value: object, probe_label: str, index: int) -> Expectation:
    label = f"{probe_label}.expectations[{index}]"
    item = _mapping(value, label)
    _keys(
        item,
        label,
        required={"type", "value"},
        optional={"weight", "gate", "aspect"},
    )
    expectation_type = _text(item["type"], f"{label}.type")
    if expectation_type not in EXPECTATION_TYPES:
        raise BenchmarkProfileError(
            f"{label}.type must be one of: {', '.join(sorted(EXPECTATION_TYPES))}."
        )
    expectation_value = _text(item["value"], f"{label}.value")
    if expectation_type == "regex":
        try:
            re.compile(expectation_value)
        except re.error as error:
            raise BenchmarkProfileError(f"{label}.value is not a valid regex: {error}") from error
    aspect = _text(item.get("aspect", "identity"), f"{label}.aspect")
    if aspect not in EXPECTATION_ASPECTS:
        raise BenchmarkProfileError(
            f"{label}.aspect must be one of: {', '.join(sorted(EXPECTATION_ASPECTS))}."
        )
    return Expectation(
        type=expectation_type,  # type: ignore[arg-type]
        value=expectation_value,
        weight=_positive_number(item.get("weight", 1.0), f"{label}.weight"),
        gate=_boolean(item.get("gate", False), f"{label}.gate"),
        aspect=aspect,  # type: ignore[arg-type]
    )


def _state_transition(value: object, label: str) -> StateTransition:
    item = _mapping(value, label)
    _keys(item, label, required={"type", "agent_identity"})
    transition_type = _text(item["type"], f"{label}.type")
    if transition_type not in TRANSITION_TYPES:
        raise BenchmarkProfileError(
            f"{label}.type must be one of: {', '.join(sorted(TRANSITION_TYPES))}."
        )
    identity = _mapping(item["agent_identity"], f"{label}.agent_identity")
    return StateTransition(
        type=transition_type,  # type: ignore[arg-type]
        agent_identity={
            str(key): _json_value(item, f"{label}.agent_identity.{key}")
            for key, item in identity.items()
        },
    )


def _probe_result(value: object, index: int) -> ProbeResult:
    label = f"benchmark report.results[{index}]"
    item = _mapping(value, label)
    _keys(
        item,
        label,
        required={
            "probe_id",
            "dimension",
            "score",
            "weight",
            "response",
            "expectations",
            "metadata",
            "evaluation_metadata",
            "component_scores",
            "error",
        },
    )
    response = item["response"]
    error = item["error"]
    if not isinstance(response, str):
        raise BenchmarkProfileError(f"{label}.response must be text.")
    if not isinstance(error, str):
        raise BenchmarkProfileError(f"{label}.error must be text.")
    raw_expectations = item["expectations"]
    if not isinstance(raw_expectations, list):
        raise BenchmarkProfileError(f"{label}.expectations must be a list.")
    expectations = tuple(
        _expectation_result(result, label, expectation_index)
        for expectation_index, result in enumerate(raw_expectations)
    )
    dimension = _text(item["dimension"], f"{label}.dimension")
    if dimension not in DIMENSIONS:
        raise BenchmarkProfileError(
            f"{label}.dimension must be one of: {', '.join(sorted(DIMENSIONS))}."
        )
    return ProbeResult(
        probe_id=_identifier(item["probe_id"], f"{label}.probe_id"),
        dimension=dimension,
        score=_unit_score(item["score"], f"{label}.score"),
        weight=_positive_number(item["weight"], f"{label}.weight"),
        response=response,
        expectations=expectations,
        metadata=_json_mapping(item["metadata"], f"{label}.metadata"),
        evaluation_metadata=_json_mapping(
            item["evaluation_metadata"], f"{label}.evaluation_metadata"
        ),
        component_scores=_score_mapping(
            item["component_scores"], f"{label}.component_scores"
        ),
        error=error,
    )


def _expectation_result(
    value: object, probe_label: str, index: int
) -> ExpectationResult:
    label = f"{probe_label}.expectations[{index}]"
    item = _mapping(value, label)
    _keys(item, label, required={"expectation", "passed"})
    return ExpectationResult(
        expectation=_expectation(item["expectation"], label, 0),
        passed=_boolean(item["passed"], f"{label}.passed"),
    )


def _json_mapping(value: object, label: str) -> dict[str, JsonValue]:
    item = _mapping(value, label)
    return {
        str(key): _json_value(child, f"{label}.{key}")
        for key, child in item.items()
    }


def _score_mapping(value: object, label: str) -> dict[str, float]:
    item = _mapping(value, label)
    return {
        _identifier(key, f"{label} key"): _unit_score(score, f"{label}.{key}")
        for key, score in item.items()
    }


def _unit_score(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkProfileError(f"{label} must be a number from 0 to 1.")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise BenchmarkProfileError(
            f"{label} must be a finite number from 0 to 1."
        )
    return score


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BenchmarkProfileError(f"{label} must be an object.")
    return value


def _keys(
    value: dict[str, Any],
    label: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    if missing:
        raise BenchmarkProfileError(f"{label} is missing required fields: {', '.join(missing)}.")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise BenchmarkProfileError(f"{label} has unknown fields: {', '.join(unexpected)}.")


def _nonempty_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise BenchmarkProfileError(f"{label} must be a non-empty list.")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise BenchmarkProfileError(
            f"{label} must start with an alphanumeric character and contain only letters, numbers, '.', '_', or '-'."
        )
    return text


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkProfileError(f"{label} must be non-empty text.")
    return value.strip()


def _optional_text(value: object, label: str) -> str:
    if value is None:
        return ""
    return _text(value, label)


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkProfileError(f"{label} must be a positive number.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise BenchmarkProfileError(f"{label} must be a positive finite number.")
    return number


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise BenchmarkProfileError(f"{label} must be true or false.")
    return value


def _unique(values: Any, label: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise BenchmarkProfileError(f"{label} must be unique.")


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BenchmarkProfileError(f"{label} must contain finite JSON numbers.")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{label}[]") for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item, f"{label}.{key}") for key, item in value.items()}
    raise BenchmarkProfileError(f"{label} must contain JSON-compatible values.")
