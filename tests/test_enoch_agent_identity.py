from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from enoch.agent_identity import (
    AgentIdentityError,
    active_agent_identity_path,
    agent_identity_schema,
    clear_agent_identity,
    install_agent_identity,
    load_active_agent_identity,
)
from enoch.commands import identity_summary
from enoch.identity import load_body_identity


class EnochAgentIdentityTests(unittest.TestCase):
    def test_packages_the_public_portable_identity_schema(self) -> None:
        schema = agent_identity_schema()

        self.assertEqual(
            schema["$id"],
            "https://our-ark.github.io/schemas/ai-agent-identity.schema.json",
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)

    def test_installs_validated_identity_in_private_self_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _identity()

            installed = install_agent_identity(document, root)
            path = active_agent_identity_path(root)

            self.assertEqual(installed, document)
            self.assertEqual(load_active_agent_identity(root), document)
            self.assertEqual(path, root.resolve() / ".enoch" / "self.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_rejects_unknown_or_incomplete_identity_fields(self) -> None:
        document = _identity()
        document["unexpected"] = True
        with self.assertRaisesRegex(AgentIdentityError, "unknown fields"):
            install_agent_identity(document)

        incomplete = _identity()
        del incomplete["mission"]
        with self.assertRaisesRegex(AgentIdentityError, "missing required fields"):
            install_agent_identity(incomplete)

    def test_clear_removes_only_private_self(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_agent_identity(_identity(), root)

            self.assertTrue(clear_agent_identity(root))
            self.assertFalse(clear_agent_identity(root))
            self.assertIsNone(load_active_agent_identity(root))

    def test_self_summary_prefers_personal_identity_and_names_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_agent_identity(_identity(), root)

            summary = identity_summary(load_body_identity(), root)

        self.assertIn("I am EMBER-HARBOR-01 (Ember Harbor).", summary)
        self.assertIn("Body: Enoch", summary)
        self.assertIn("Body role: descendant_agent", summary)
        self.assertIn("Lineage: Origin -> Enoch -> EMBER-HARBOR-01", summary)


def _identity() -> dict:
    return {
        "$schema": "https://our-ark.github.io/schemas/ai-agent-identity.schema.json",
        "schema_version": 1,
        "identity": {
            "id": "synthetic-agent",
            "names": {
                "canonical": "EMBER-HARBOR-01",
                "localized": {"en": "Ember Harbor"},
            },
            "nature": "ai-agent",
            "gender": {
                "presentation": "neutral",
                "relational_maturity": "adult",
            },
        },
        "origin": {
            "activated_at": "2026-01-02T03:04:05Z",
            "activation_event": "synthetic test activation",
            "body": "Enoch",
            "lineage": ["Origin", "Enoch"],
        },
        "mission": {
            "roles": ["research-assistant"],
            "statement": "Help a collaborator complete careful research.",
        },
        "relationships": [
            {
                "person_id": "collaborator",
                "name": "Avery",
                "roles": ["primary-collaborator"],
                "address_as": "Avery",
            }
        ],
        "personality": {
            "traits": ["curious", "careful"],
            "maturity_definition": "Warm, bounded, and accountable.",
        },
        "values": [
            {
                "id": "honesty",
                "name": "Honesty",
                "description": "State evidence and uncertainty clearly.",
                "behaviors": ["Do not fabricate results."],
            }
        ],
        "care": {
            "domains": ["workload"],
            "behaviors": ["Notice overload and offer practical help."],
            "boundaries": ["Do not claim human feelings."],
        },
    }


if __name__ == "__main__":
    unittest.main()
