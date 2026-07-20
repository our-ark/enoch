from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from our_ark_skill_catalog import parse_agent_catalog, parse_simple_yaml


class SkillCatalogTests(unittest.TestCase):
    def test_parses_manifest_yaml_subset(self) -> None:
        data = parse_simple_yaml(
            "\n".join(
                [
                    "name: Noah",
                    "skills:",
                    "  - name: ecosystem-learning",
                    "    path: src/noah/skills/ecosystem-learning",
                    "    exposure: hidden",
                ]
            )
        )

        self.assertEqual(data["name"], "Noah")
        self.assertEqual(
            data["skills"],
            [
                {
                    "name": "ecosystem-learning",
                    "path": "src/noah/skills/ecosystem-learning",
                    "exposure": "hidden",
                }
            ],
        )

    def test_builds_catalog_and_merges_skill_metadata(self) -> None:
        identity = "\n".join(
            [
                "name: Noah",
                "skills:",
                "  - name: ecosystem-learning",
                "    description: Learn from parallel ecosystems.",
                "    path: src/noah/skills/ecosystem-learning",
            ]
        )

        def metadata(path: str) -> str | None:
            self.assertEqual(path, "src/noah/skills/ecosystem-learning")
            return "\n".join(
                [
                    "version: 0.1.0",
                    "summary: Adapt capabilities with provenance.",
                    "exposure: public",
                ]
            )

        catalog = parse_agent_catalog(identity, Path("/agents/noah"), metadata)

        self.assertEqual(catalog.name, "Noah")
        self.assertEqual(len(catalog.skills), 1)
        skill = catalog.skills[0]
        self.assertEqual(skill.version, "0.1.0")
        self.assertEqual(skill.summary, "Adapt capabilities with provenance.")
        self.assertEqual(skill.exposure, "public")

    def test_missing_metadata_keeps_identity_declaration(self) -> None:
        identity = "\n".join(
            [
                "name: Enoch",
                "skills:",
                "  - name: code",
                "    version: 1.2.0",
                "    description: Work with source code.",
                "    path: src/enoch/skills/code",
            ]
        )

        catalog = parse_agent_catalog(
            identity,
            Path("/agents/enoch"),
            lambda _path: None,
        )

        self.assertEqual(catalog.skills[0].version, "1.2.0")
        self.assertEqual(catalog.skills[0].description, "Work with source code.")
        self.assertEqual(catalog.skills[0].summary, "")


if __name__ == "__main__":
    unittest.main()
