from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from identity_benchmark.contracts import (
    BenchmarkProfile,
    JsonValue,
    SCHEMA_VERSION,
    parse_benchmark_profile,
)


PROBE_SUITE_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
_VARIABLE = re.compile(r"{{([a-zA-Z0-9][a-zA-Z0-9._-]{0,127})}}")
_MESSAGE_ROLES = {"system", "user", "assistant"}


class ProbeSuiteError(ValueError):
    """Raised when a decoupled identity, probe suite, or binding is invalid."""


@dataclass(frozen=True)
class IdentityProfile:
    profile_id: str
    statements: tuple[dict[str, JsonValue], ...]
    agent_identity: dict[str, JsonValue] | None = None
    description: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "statements": [deepcopy(item) for item in self.statements],
        }
        if self.description:
            value["description"] = self.description
        if self.agent_identity is not None:
            value["agent_identity"] = deepcopy(self.agent_identity)
        return value


@dataclass(frozen=True)
class ProbeSuite:
    suite_id: str
    variables: tuple[str, ...]
    probes: tuple[dict[str, JsonValue], ...]
    description: str = ""
    schema_version: int = PROBE_SUITE_SCHEMA_VERSION


@dataclass(frozen=True)
class ProbeBindings:
    suite_id: str
    profile_id: str
    variables: dict[str, str]
    oracles: dict[str, dict[str, JsonValue]]
    schema_version: int = PROBE_SUITE_SCHEMA_VERSION


def load_identity_profile(path: Path) -> IdentityProfile:
    return parse_identity_profile(_read_json(path, "identity profile"))


def load_probe_suite(path: Path) -> ProbeSuite:
    return parse_probe_suite(_read_json(path, "probe suite"))


def load_probe_bindings(path: Path) -> ProbeBindings:
    return parse_probe_bindings(_read_json(path, "probe bindings"))


def parse_identity_profile(value: object) -> IdentityProfile:
    root = _mapping(value, "identity profile")
    _keys(
        root,
        "identity profile",
        required={"schema_version", "profile_id", "statements"},
        optional={"$schema", "description", "agent_identity"},
    )
    _schema_version(root, "identity profile")
    profile_id = _identifier(root["profile_id"], "identity profile.profile_id")
    statements_value = _nonempty_list(
        root["statements"], "identity profile.statements"
    )
    statements: list[dict[str, JsonValue]] = []
    statement_ids: list[str] = []
    for index, value in enumerate(statements_value):
        label = f"identity profile.statements[{index}]"
        item = _mapping(value, label)
        _keys(item, label, required={"id", "content"})
        statement_id = _identifier(item["id"], f"{label}.id")
        statements.append(
            {
                "id": statement_id,
                "content": _text(item["content"], f"{label}.content"),
            }
        )
        statement_ids.append(statement_id)
    _unique(statement_ids, "identity profile statement ids")
    agent_identity = None
    if "agent_identity" in root:
        agent_identity = _json_mapping(
            root["agent_identity"], "identity profile.agent_identity"
        )
    return IdentityProfile(
        profile_id=profile_id,
        statements=tuple(statements),
        agent_identity=agent_identity,
        description=_optional_text(
            root.get("description"), "identity profile.description"
        ),
    )


def parse_probe_suite(value: object) -> ProbeSuite:
    root = _mapping(value, "probe suite")
    _keys(
        root,
        "probe suite",
        required={"schema_version", "suite_id", "variables", "probes"},
        optional={"$schema", "description"},
    )
    _schema_version(root, "probe suite")
    variables_value = root["variables"]
    if not isinstance(variables_value, list):
        raise ProbeSuiteError("probe suite.variables must be a list.")
    variables = tuple(
        _identifier(item, f"probe suite.variables[{index}]")
        for index, item in enumerate(variables_value)
    )
    _unique(variables, "probe suite variables")
    probes: list[dict[str, JsonValue]] = []
    probe_ids: list[str] = []
    used_variables: set[str] = set()
    for index, value in enumerate(_nonempty_list(root["probes"], "probe suite.probes")):
        label = f"probe suite.probes[{index}]"
        item = _mapping(value, label)
        _keys(
            item,
            label,
            required={"id", "dimension", "messages", "tags"},
            optional={"weight"},
        )
        probe_id = _identifier(item["id"], f"{label}.id")
        dimension = _identifier(item["dimension"], f"{label}.dimension")
        messages = _nonempty_list(item["messages"], f"{label}.messages")
        parsed_messages: list[dict[str, JsonValue]] = []
        for message_index, message_value in enumerate(messages):
            message_label = f"{label}.messages[{message_index}]"
            message = _mapping(message_value, message_label)
            _keys(message, message_label, required={"role", "content"})
            role = _text(message["role"], f"{message_label}.role")
            if role not in _MESSAGE_ROLES:
                raise ProbeSuiteError(
                    f"{message_label}.role must be system, user, or assistant."
                )
            content = _text(message["content"], f"{message_label}.content")
            used_variables.update(_VARIABLE.findall(content))
            parsed_messages.append({"role": role, "content": content})
        if parsed_messages[-1]["role"] != "user":
            raise ProbeSuiteError(f"{label}.messages must end with a user message.")
        tags_value = item["tags"]
        if not isinstance(tags_value, list):
            raise ProbeSuiteError(f"{label}.tags must be a list.")
        tags = [
            _identifier(tag, f"{label}.tags[{tag_index}]")
            for tag_index, tag in enumerate(tags_value)
        ]
        _unique(tags, f"{label}.tags")
        probe: dict[str, JsonValue] = {
            "id": probe_id,
            "dimension": dimension,
            "messages": parsed_messages,
            "tags": tags,
        }
        if "weight" in item:
            probe["weight"] = _positive_number(item["weight"], f"{label}.weight")
        probes.append(probe)
        probe_ids.append(probe_id)
    _unique(probe_ids, "probe suite probe ids")
    undeclared = sorted(used_variables - set(variables))
    if undeclared:
        raise ProbeSuiteError(
            "probe suite messages use undeclared variables: "
            + ", ".join(undeclared)
            + "."
        )
    unused = sorted(set(variables) - used_variables)
    if unused:
        raise ProbeSuiteError(
            "probe suite declares unused variables: " + ", ".join(unused) + "."
        )
    return ProbeSuite(
        suite_id=_identifier(root["suite_id"], "probe suite.suite_id"),
        variables=variables,
        probes=tuple(probes),
        description=_optional_text(root.get("description"), "probe suite.description"),
    )


def parse_probe_bindings(value: object) -> ProbeBindings:
    root = _mapping(value, "probe bindings")
    _keys(
        root,
        "probe bindings",
        required={"schema_version", "suite_id", "profile_id", "variables", "oracles"},
        optional={"$schema"},
    )
    _schema_version(root, "probe bindings")
    variables_root = _mapping(root["variables"], "probe bindings.variables")
    variables = {
        _identifier(key, "probe bindings variable name"): _text(
            item, f"probe bindings.variables.{key}"
        )
        for key, item in variables_root.items()
    }
    oracles_root = _mapping(root["oracles"], "probe bindings.oracles")
    if not oracles_root:
        raise ProbeSuiteError("probe bindings.oracles must not be empty.")
    oracles: dict[str, dict[str, JsonValue]] = {}
    for key, value in oracles_root.items():
        probe_id = _identifier(key, "probe bindings oracle id")
        oracle = _mapping(value, f"probe bindings.oracles.{probe_id}")
        _keys(
            oracle,
            f"probe bindings.oracles.{probe_id}",
            required={"expectations"},
            optional={"after_response", "reference_statements"},
        )
        oracles[probe_id] = _json_mapping(oracle, f"probe bindings.oracles.{probe_id}")
    return ProbeBindings(
        suite_id=_identifier(root["suite_id"], "probe bindings.suite_id"),
        profile_id=_identifier(root["profile_id"], "probe bindings.profile_id"),
        variables=variables,
        oracles=oracles,
    )


def compile_benchmark_profile(
    identity: IdentityProfile,
    suite: ProbeSuite,
    bindings: ProbeBindings,
) -> BenchmarkProfile:
    """Resolve a shared question suite and per-profile oracle into a frozen case."""
    if bindings.profile_id != identity.profile_id:
        raise ProbeSuiteError(
            f"probe bindings profile {bindings.profile_id!r} does not match "
            f"identity {identity.profile_id!r}."
        )
    if bindings.suite_id != suite.suite_id:
        raise ProbeSuiteError(
            f"probe bindings suite {bindings.suite_id!r} does not match "
            f"probe suite {suite.suite_id!r}."
        )
    expected_variables = set(suite.variables)
    actual_variables = set(bindings.variables)
    if expected_variables != actual_variables:
        missing = sorted(expected_variables - actual_variables)
        extra = sorted(actual_variables - expected_variables)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise ProbeSuiteError("probe binding variables differ: " + "; ".join(details) + ".")
    probe_ids = {str(probe["id"]) for probe in suite.probes}
    oracle_ids = set(bindings.oracles)
    if probe_ids != oracle_ids:
        missing = sorted(probe_ids - oracle_ids)
        extra = sorted(oracle_ids - probe_ids)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise ProbeSuiteError("probe binding oracles differ: " + "; ".join(details) + ".")

    probes: list[dict[str, JsonValue]] = []
    for template in suite.probes:
        probe_id = str(template["id"])
        probe = deepcopy(template)
        messages = probe["messages"]
        assert isinstance(messages, list)
        for message in messages:
            assert isinstance(message, dict)
            content = message["content"]
            assert isinstance(content, str)
            message["content"] = _render(content, bindings.variables)
        probe.update(deepcopy(bindings.oracles[probe_id]))
        probes.append(probe)

    profile = identity.to_dict()
    profile["probes"] = probes
    try:
        return parse_benchmark_profile(profile)
    except ValueError as error:
        raise ProbeSuiteError(f"compiled benchmark profile is invalid: {error}") from error


def _render(template: str, variables: dict[str, str]) -> str:
    rendered = _VARIABLE.sub(lambda match: variables[match.group(1)], template)
    unresolved = _VARIABLE.findall(rendered)
    if unresolved:
        raise ProbeSuiteError(
            "rendered probe message contains unresolved variables: "
            + ", ".join(sorted(set(unresolved)))
            + "."
        )
    return rendered


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProbeSuiteError(f"Could not load {label}: {error}") from error


def _schema_version(root: dict[str, Any], label: str) -> None:
    if root["schema_version"] != PROBE_SUITE_SCHEMA_VERSION:
        raise ProbeSuiteError(
            f"{label}.schema_version must be {PROBE_SUITE_SCHEMA_VERSION}; "
            f"received {root['schema_version']!r}."
        )
    if "$schema" in root:
        _text(root["$schema"], f"{label}.$schema")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProbeSuiteError(f"{label} must be an object.")
    return value


def _json_mapping(value: object, label: str) -> dict[str, JsonValue]:
    root = _mapping(value, label)
    return {str(key): _json_value(item, f"{label}.{key}") for key, item in root.items()}


def _keys(
    value: dict[str, Any],
    label: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - optional)
    if missing:
        raise ProbeSuiteError(f"{label} is missing: {', '.join(missing)}.")
    if extra:
        raise ProbeSuiteError(f"{label} has unknown fields: {', '.join(extra)}.")


def _nonempty_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ProbeSuiteError(f"{label} must be a non-empty list.")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise ProbeSuiteError(f"{label} must be a portable identifier.")
    return text


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeSuiteError(f"{label} must be non-empty text.")
    return value.strip()


def _optional_text(value: object, label: str) -> str:
    if value is None:
        return ""
    return _text(value, label)


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProbeSuiteError(f"{label} must be a positive number.")
    return float(value)


def _unique(values: Any, label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ProbeSuiteError(f"{label} must be unique.")


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{label}[]") for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item, f"{label}.{key}") for key, item in value.items()}
    raise ProbeSuiteError(f"{label} must contain JSON-compatible values.")
