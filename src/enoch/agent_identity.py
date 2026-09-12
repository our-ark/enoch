from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from importlib import resources
import json
import os
from pathlib import Path
from typing import Any

from enoch.paths import private_state_path
from enoch.state import atomic_write


SCHEMA_VERSION = 1
SCHEMA_ID = "https://our-ark.github.io/schemas/ai-agent-identity.schema.json"
SCHEMA_RESOURCE = ("schemas", "ai-agent-identity.schema.json")
SELF_FILENAME = "self.json"


class AgentIdentityError(ValueError):
    """Raised when a portable personal Agent Identity is malformed."""


@lru_cache(maxsize=1)
def agent_identity_schema() -> dict[str, Any]:
    """Load Enoch's packaged copy of the portable Agent Identity schema."""
    target = resources.files("enoch")
    for part in SCHEMA_RESOURCE:
        target = target.joinpath(part)
    try:
        schema = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AgentIdentityError(
            f"Could not load packaged Agent Identity schema: {error}"
        ) from error
    if not isinstance(schema, dict) or schema.get("$id") != SCHEMA_ID:
        raise AgentIdentityError("Packaged Agent Identity schema has an invalid $id.")
    return schema


def load_agent_identity(path: Path) -> dict[str, Any]:
    """Load and validate one portable AI Agent Identity JSON document."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise AgentIdentityError(f"Invalid Agent Identity JSON: {error}") from error
    return validate_agent_identity(document)


def active_agent_identity_path(root: Path | None = None) -> Path:
    """Return the private personal identity contract for one Enoch instance."""
    return private_state_path(SELF_FILENAME, root)


def load_active_agent_identity(root: Path | None = None) -> dict[str, Any] | None:
    path = active_agent_identity_path(root)
    return load_agent_identity(path) if path.exists() else None


def install_agent_identity(
    document: object,
    root: Path | None = None,
) -> dict[str, Any]:
    """Validate and atomically install one private personal identity."""
    validated = validate_agent_identity(document)
    path = active_agent_identity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    atomic_write(
        path,
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
    )
    os.chmod(path, 0o600)
    return validated


def clear_agent_identity(root: Path | None = None) -> bool:
    path = active_agent_identity_path(root)
    if not path.exists():
        return False
    path.unlink()
    return True


def active_agent_identity_for_prompt(root: Path | None = None) -> str:
    document = load_active_agent_identity(root)
    if document is None:
        return ""
    identity = document["identity"]
    names = identity["names"]
    gender = identity["gender"]
    origin = document["origin"]
    mission = document["mission"]
    relationships = "\n".join(
        "- "
        + relationship["name"]
        + f"; roles: {', '.join(relationship['roles'])}; "
        + f"address as: {relationship['address_as']}"
        for relationship in document["relationships"]
    )
    values = "\n".join(
        "- "
        + value["name"]
        + f": {value['description']} Behaviors: {'; '.join(value['behaviors'])}"
        for value in document["values"]
    )
    care = document["care"]
    localized = ", ".join(
        f"{locale}: {name}" for locale, name in names["localized"].items()
    )
    return f"""Personal name: {names['canonical']}
Localized names: {localized}
Nature: {identity['nature']}
Gender presentation: {gender['presentation']}
Relational maturity: {gender['relational_maturity']}
Activated at: {origin['activated_at']}
Activation event: {origin['activation_event']}
Code body: {origin['body']}
Lineage: {' -> '.join(origin['lineage'])}

Mission roles: {', '.join(mission['roles'])}
Mission: {mission['statement']}

Relationships:
{relationships}

Personality: {', '.join(document['personality']['traits'])}
Maturity definition: {document['personality']['maturity_definition']}

Values:
{values}

Care domains: {', '.join(care['domains'])}
Care behaviors: {'; '.join(care['behaviors'])}
Care boundaries: {'; '.join(care['boundaries'])}"""


def validate_agent_identity(document: object) -> dict[str, Any]:
    """Validate the v1 portable personal Agent Identity contract."""
    agent_identity_schema()
    root = _mapping(document, "identity document")
    _keys(
        root,
        "identity document",
        required={
            "schema_version",
            "identity",
            "origin",
            "mission",
            "relationships",
            "personality",
            "values",
            "care",
        },
        optional={"$schema"},
    )
    _exact_version(root.get("schema_version"))
    if "$schema" in root:
        _text(root["$schema"], "$schema")

    identity = _mapping(root.get("identity"), "identity")
    _keys(identity, "identity", required={"id", "names", "nature", "gender"})
    _text(identity.get("id"), "identity.id")
    names = _mapping(identity.get("names"), "identity.names")
    _keys(names, "identity.names", required={"canonical", "localized"})
    _text(names.get("canonical"), "identity.names.canonical")
    localized = _mapping(names.get("localized"), "identity.names.localized")
    if not localized:
        raise AgentIdentityError("identity.names.localized must not be empty.")
    for locale, name in localized.items():
        _text(locale, "identity.names.localized locale")
        _text(name, f"identity.names.localized.{locale}")
    _text(identity.get("nature"), "identity.nature")
    gender = _mapping(identity.get("gender"), "identity.gender")
    _keys(
        gender,
        "identity.gender",
        required={"presentation", "relational_maturity"},
    )
    _text(gender.get("presentation"), "identity.gender.presentation")
    _text(gender.get("relational_maturity"), "identity.gender.relational_maturity")

    origin = _mapping(root.get("origin"), "origin")
    _keys(
        origin,
        "origin",
        required={"activated_at", "activation_event", "body", "lineage"},
    )
    _timestamp(origin.get("activated_at"), "origin.activated_at")
    _text(origin.get("activation_event"), "origin.activation_event")
    _text(origin.get("body"), "origin.body")
    _text_list(origin.get("lineage"), "origin.lineage")

    mission = _mapping(root.get("mission"), "mission")
    _keys(mission, "mission", required={"roles", "statement"})
    _text_list(mission.get("roles"), "mission.roles")
    _text(mission.get("statement"), "mission.statement")

    relationships = _mapping_list(root.get("relationships"), "relationships")
    _unique_ids(relationships, "person_id", "relationships")
    for index, relationship in enumerate(relationships):
        prefix = f"relationships[{index}]"
        _keys(
            relationship,
            prefix,
            required={"person_id", "name", "roles", "address_as"},
        )
        _text(relationship.get("person_id"), f"{prefix}.person_id")
        _text(relationship.get("name"), f"{prefix}.name")
        _text_list(relationship.get("roles"), f"{prefix}.roles")
        _text(relationship.get("address_as"), f"{prefix}.address_as")

    personality = _mapping(root.get("personality"), "personality")
    _keys(
        personality,
        "personality",
        required={"traits", "maturity_definition"},
    )
    _text_list(personality.get("traits"), "personality.traits")
    _text(personality.get("maturity_definition"), "personality.maturity_definition")

    values = _mapping_list(root.get("values"), "values")
    _unique_ids(values, "id", "values")
    for index, value in enumerate(values):
        prefix = f"values[{index}]"
        _keys(value, prefix, required={"id", "name", "description", "behaviors"})
        _text(value.get("id"), f"{prefix}.id")
        _text(value.get("name"), f"{prefix}.name")
        _text(value.get("description"), f"{prefix}.description")
        _text_list(value.get("behaviors"), f"{prefix}.behaviors")

    care = _mapping(root.get("care"), "care")
    _keys(care, "care", required={"domains", "behaviors", "boundaries"})
    _text_list(care.get("domains"), "care.domains")
    _text_list(care.get("behaviors"), "care.behaviors")
    _text_list(care.get("boundaries"), "care.boundaries")
    return deepcopy(root)


def _exact_version(value: object) -> None:
    if value != SCHEMA_VERSION:
        raise AgentIdentityError(
            f"schema_version must be {SCHEMA_VERSION}; received {value!r}."
        )


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentIdentityError(f"{label} must be an object.")
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
        raise AgentIdentityError(
            f"{label} is missing required fields: {', '.join(missing)}."
        )
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise AgentIdentityError(
            f"{label} has unknown fields: {', '.join(unexpected)}."
        )


def _mapping_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AgentIdentityError(f"{label} must be a non-empty list.")
    return [_mapping(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentIdentityError(f"{label} must be non-empty text.")
    return value.strip()


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AgentIdentityError(f"{label} must be a non-empty list.")
    cleaned = [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(set(cleaned)) != len(cleaned):
        raise AgentIdentityError(f"{label} must not contain duplicates.")
    return cleaned


def _timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AgentIdentityError(f"{label} must be an ISO 8601 timestamp.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise AgentIdentityError(f"{label} must include a UTC offset.")
    return timestamp


def _unique_ids(items: list[dict[str, Any]], key: str, label: str) -> None:
    identifiers = [_text(item.get(key), f"{label}.{key}") for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise AgentIdentityError(f"{label} must use unique {key} values.")
