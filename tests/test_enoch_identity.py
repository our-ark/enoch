from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch import load_identity


class EnochIdentityTests(unittest.TestCase):
    def test_loads_identity_from_body(self) -> None:
        identity = load_identity()

        self.assertEqual(identity.name, "Enoch")
        self.assertEqual(identity.role, "descendant_agent")
        self.assertEqual(identity.generation, 3)
        self.assertEqual(identity.ancestor, "Seth")
        self.assertEqual(identity.origin.ark, "Our-Ark")
        self.assertEqual(identity.origin.created_by, "Genesis")
        self.assertEqual(identity.body.source_path, "src/enoch")

    def test_identity_has_human_ancestry_signal(self) -> None:
        identity = load_identity()

        self.assertTrue(identity.mission)

    def test_identity_yaml_is_the_only_versioned_identity_source(self) -> None:
        self.assertTrue((ROOT / "src" / "enoch" / "identity.yaml").exists())
        self.assertFalse((ROOT / "memory" / "identity.md").exists())

    def test_identity_declares_evolution_constraints(self) -> None:
        identity = load_identity()

        self.assertIn("Treat code as body and Git history as lineage.", identity.principles)
        self.assertIn("Change code through direct human requests, tests, and human review.", identity.principles)
        self.assertIn("Prefer fewer manual commands and more natural agency.", identity.principles)

    def test_identity_declares_skills(self) -> None:
        text = (ROOT / "src" / "enoch" / "identity.yaml").read_text(encoding="utf-8")

        for name in ["telegram-talk", "code", "github", "inherit", "learn", "teach"]:
            self.assertIn(f"- name: {name}", text)
        self.assertIn("exposure: hidden", text)
        self.assertNotIn("- name: talk\n", text)
        self.assertNotIn("- name: telegram\n", text)


if __name__ == "__main__":
    unittest.main()
