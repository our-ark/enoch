from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.identity import _parse_enoch_yaml
from enoch.skills import (
    SkillsError,
    _parse_simple_yaml,
    _published_text,
    load_agent_skills,
    skills_command,
)


class EnochSkillsTests(unittest.TestCase):
    def test_loads_enoch_skills_from_identity_and_skill_metadata(self) -> None:
        agent = load_agent_skills(root=ROOT)

        names = [skill.name for skill in agent.skills]
        self.assertEqual(agent.name, "Enoch")
        self.assertIn("skill-library", names)
        self.assertIn("code", names)
        self.assertIn("inherit", names)
        self.assertIn("work", names)
        self.assertIn("learn", names)
        self.assertIn("evolve", names)
        self.assertIn("teach", names)
        inherit = next(skill for skill in agent.skills if skill.name == "inherit")
        self.assertIn("ancestor skills", inherit.summary)
        work = next(skill for skill in agent.skills if skill.name == "work")
        self.assertIn("persistent work", work.summary)
        learn = next(skill for skill in agent.skills if skill.name == "learn")
        self.assertIn("Adapt a published skill", learn.summary)
        evolve = next(skill for skill in agent.skills if skill.name == "evolve")
        self.assertIn("self-evolution", evolve.summary)
        teach = next(skill for skill in agent.skills if skill.name == "teach")
        self.assertEqual(teach.exposure, "hidden")
        self.assertIn("Hidden skill", teach.summary)
        library = next(skill for skill in agent.skills if skill.name == "skill-library")
        self.assertIn("immutable public libraries", library.summary)

    def test_identity_skill_versions_match_skill_manifests(self) -> None:
        identity = _parse_enoch_yaml(
            (ROOT / "src" / "enoch" / "identity.yaml").read_text(encoding="utf-8")
        )

        for declared in identity["skills"]:
            metadata = _parse_simple_yaml(
                (ROOT / declared["path"] / "skill.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                declared.get("version"),
                metadata.get("version"),
                declared["name"],
            )

    def test_loads_packaged_self_skills_without_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = load_agent_skills(root=Path(directory))

        names = [skill.name for skill in agent.skills]
        self.assertEqual(agent.name, "Enoch")
        self.assertIn("skill-library", names)
        self.assertIn("code", names)
        self.assertIn("inherit", names)
        self.assertIn("work", names)
        self.assertIn("evolve", names)
        self.assertIn("teach", names)
        learn = next(skill for skill in agent.skills if skill.name == "learn")
        self.assertTrue(learn.version)
        self.assertIn("Adapt a published skill", learn.summary)

    @unittest.skipUnless(
        (ROOT / "libraries" / "telegram-vision").is_dir(),
        "shared library sources are outside the inheritable agent body",
    )
    def test_external_vision_skill_records_direct_parent_lineage(self) -> None:
        metadata = (
            ROOT / "libraries" / "telegram-vision" / "skills" / "telegram-vision" / "skill.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("origin_agent: Eve", metadata)
        self.assertIn("inherited_from: Seth", metadata)
        self.assertIn("source_commit: f0fa336", metadata)
        self.assertIn("library_contract: telegram-vision/v1", metadata)

    def test_skill_library_declares_shared_library_contract(self) -> None:
        metadata = (
            ROOT / "src" / "enoch" / "skills" / "skill-library" / "skill.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("owner: Enoch", metadata)
        self.assertIn("contract: skill-library/v1", metadata)
        self.assertIn("implementation: procedural", metadata)

    def test_skills_command_can_inspect_explicit_local_agent_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            enoch = workspace / "enoch"
            lucy = workspace / "lucy"
            (enoch / "src" / "enoch").mkdir(parents=True)
            (lucy / "src" / "lucy" / "skills" / "teach").mkdir(parents=True)
            (lucy / "src" / "lucy" / "identity.yaml").write_text(
                "\n".join(
                    [
                        "name: Lucy",
                        "skills:",
                        "  - name: teach",
                        "    description: Package lessons.",
                        "    path: src/lucy/skills/teach",
                    ]
                ),
                encoding="utf-8",
            )
            (lucy / "src" / "lucy" / "skills" / "teach" / "skill.yaml").write_text(
                "summary: Package Lucy lessons.\n",
                encoding="utf-8",
            )

            output = skills_command(f"skills {lucy}", enoch, prefix="")

        self.assertIn("Lucy skills:", output)
        self.assertIn("teach", output)
        self.assertIn("Package Lucy lessons.", output)

    def test_skills_command_reads_named_agent_from_github_main(self) -> None:
        def published_text(agent: str, path: str, **_kwargs) -> str:
            self.assertEqual(agent, "lucy")
            if path == "src/lucy/identity.yaml":
                return "\n".join(
                    [
                        "name: Lucy",
                        "skills:",
                        "  - name: teach",
                        "    description: Package lessons.",
                        "    path: src/lucy/skills/teach",
                    ]
                )
            if path == "src/lucy/skills/teach/skill.yaml":
                return "summary: Package Lucy lessons.\n"
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "instances" / "enoch-gary"
            with patch("enoch.skills.catalog._published_text", side_effect=published_text):
                output = skills_command("skills lucy", root, prefix="")

        self.assertIn("Lucy skills:", output)
        self.assertIn("Root: our-ark/lucy@main", output)
        self.assertIn("teach", output)
        self.assertIn("Package Lucy lessons.", output)

    def test_skills_command_reads_any_named_agent_from_github_main(self) -> None:
        def published_text(agent: str, path: str, **_kwargs) -> str:
            self.assertEqual(agent, "adam")
            if path == "src/adam/identity.yaml":
                return "\n".join(
                    [
                        "name: Adam",
                        "skills:",
                        "  - name: inherit",
                        "    description: Learn from direct ancestors.",
                        "    path: src/adam/skills/inherit",
                    ]
                )
            if path == "src/adam/skills/inherit/skill.yaml":
                return "summary: Inherit ancestor skills.\n"
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "instances" / "enoch-gary"
            with patch("enoch.skills.catalog._published_text", side_effect=published_text):
                output = skills_command("skills adam", root, prefix="")

        self.assertIn("Adam skills:", output)
        self.assertIn("Root: our-ark/adam@main", output)
        self.assertIn("inherit", output)

    def test_skills_command_reports_missing_published_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("enoch.skills.catalog._published_text", side_effect=SkillsError("boom")):
                output = skills_command("skills missing", Path(directory), prefix="")

        self.assertIn("Enoch could not inspect skills", output)

    def test_skills_command_reports_missing_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = skills_command("skills ./missing", Path(directory), prefix="")

        self.assertIn("Enoch could not inspect skills", output)

    def test_skills_command_keeps_named_agent_independent_from_local_checkout(self) -> None:
        def published_text(agent: str, path: str, **_kwargs) -> str:
            if path == "src/adam/identity.yaml":
                return "\n".join(
                    [
                        "name: Adam",
                        "skills:",
                        "  - name: github-main",
                        "    description: Published skill.",
                        "    path: src/adam/skills/github-main",
                    ]
                )
            if path == "src/adam/skills/github-main/skill.yaml":
                return "summary: Published main skill.\n"
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            enoch = workspace / "instances" / "enoch-gary"
            adam = workspace / "adam"
            (adam / "src" / "adam").mkdir(parents=True)
            (adam / "src" / "adam" / "identity.yaml").write_text(
                "\n".join(
                    [
                        "name: Adam",
                        "skills:",
                        "  - name: local-dirty",
                        "    description: Local unpublished skill.",
                        "    path: src/adam/skills/local-dirty",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("enoch.skills.catalog._published_text", side_effect=published_text):
                output = skills_command("skills adam", enoch, prefix="")

        self.assertIn("Adam skills:", output)
        self.assertIn("github-main", output)
        self.assertNotIn("local-dirty", output)

    @patch("enoch.skills.catalog.load_provider")
    def test_published_text_uses_configured_forge(self, load_provider) -> None:
        provider = load_provider.return_value
        provider.read_text.return_value = "name: Lucy\n"

        text = _published_text("lucy", "src/lucy/identity.yaml")

        self.assertEqual(text, "name: Lucy\n")
        load_provider.assert_called_once_with("forge", None)
        provider.read_text.assert_called_once_with(
            "our-ark/lucy",
            "src/lucy/identity.yaml",
            "main",
        )


if __name__ == "__main__":
    unittest.main()
