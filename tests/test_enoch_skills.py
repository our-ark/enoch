from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.skills import SkillsError, _github_text, load_agent_skills, skills_command


class EnochSkillsTests(unittest.TestCase):
    def test_loads_enoch_skills_from_identity_and_skill_metadata(self) -> None:
        agent = load_agent_skills(root=ROOT)

        names = [skill.name for skill in agent.skills]
        self.assertEqual(agent.name, "Enoch")
        self.assertIn("telegram-talk", names)
        self.assertIn("telegram-vision", names)
        self.assertIn("code", names)
        self.assertIn("inherit", names)
        self.assertIn("work", names)
        self.assertIn("learn", names)
        self.assertIn("evolve", names)
        self.assertIn("teach", names)
        inherit = next(skill for skill in agent.skills if skill.name == "inherit")
        self.assertEqual(inherit.version, "0.1.0")
        self.assertIn("ancestor skills", inherit.summary)
        work = next(skill for skill in agent.skills if skill.name == "work")
        self.assertEqual(work.version, "0.1.0")
        self.assertIn("persistent work", work.summary)
        learn = next(skill for skill in agent.skills if skill.name == "learn")
        self.assertEqual(learn.version, "0.1.0")
        self.assertIn("Adapt a published skill", learn.summary)
        evolve = next(skill for skill in agent.skills if skill.name == "evolve")
        self.assertEqual(evolve.version, "0.2.0")
        self.assertIn("self-evolution", evolve.summary)
        teach = next(skill for skill in agent.skills if skill.name == "teach")
        self.assertEqual(teach.exposure, "hidden")
        self.assertIn("Hidden skill", teach.summary)
        vision = next(skill for skill in agent.skills if skill.name == "telegram-vision")
        self.assertEqual(vision.version, "0.1.0")
        self.assertIn("photos", vision.summary)

    def test_loads_packaged_self_skills_without_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = load_agent_skills(root=Path(directory))

        names = [skill.name for skill in agent.skills]
        self.assertEqual(agent.name, "Enoch")
        self.assertIn("telegram-talk", names)
        self.assertIn("telegram-vision", names)
        self.assertIn("code", names)
        self.assertIn("inherit", names)
        self.assertIn("work", names)
        self.assertIn("evolve", names)
        self.assertIn("teach", names)
        learn = next(skill for skill in agent.skills if skill.name == "learn")
        self.assertEqual(learn.version, "0.1.0")
        self.assertIn("Adapt a published skill", learn.summary)

    def test_telegram_vision_records_direct_parent_lineage(self) -> None:
        metadata = (
            ROOT / "src" / "enoch" / "skills" / "telegram-vision" / "skill.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("origin_agent: Eve", metadata)
        self.assertIn("inherited_from: Seth", metadata)
        self.assertIn("source_commit: f0fa336", metadata)

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
        def github_text(agent: str, path: str) -> str:
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
            with patch("enoch.skills._github_text", side_effect=github_text):
                output = skills_command("skills lucy", root, prefix="")

        self.assertIn("Lucy skills:", output)
        self.assertIn("Root: github.com/our-ark/lucy@main", output)
        self.assertIn("teach", output)
        self.assertIn("Package Lucy lessons.", output)

    def test_skills_command_reads_any_named_agent_from_github_main(self) -> None:
        def github_text(agent: str, path: str) -> str:
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
            with patch("enoch.skills._github_text", side_effect=github_text):
                output = skills_command("skills adam", root, prefix="")

        self.assertIn("Adam skills:", output)
        self.assertIn("Root: github.com/our-ark/adam@main", output)
        self.assertIn("inherit", output)

    def test_skills_command_reports_missing_published_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("enoch.skills._github_text", side_effect=SkillsError("boom")):
                output = skills_command("skills missing", Path(directory), prefix="")

        self.assertIn("Enoch could not inspect skills", output)

    def test_skills_command_reports_missing_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = skills_command("skills ./missing", Path(directory), prefix="")

        self.assertIn("Enoch could not inspect skills", output)

    def test_skills_command_keeps_named_agent_independent_from_local_checkout(self) -> None:
        def github_text(agent: str, path: str) -> str:
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

            with patch("enoch.skills._github_text", side_effect=github_text):
                output = skills_command("skills adam", enoch, prefix="")

        self.assertIn("Adam skills:", output)
        self.assertIn("github-main", output)
        self.assertNotIn("local-dirty", output)

    @patch("enoch.skills.subprocess.run")
    @patch("enoch.skills.shutil.which", return_value="/usr/local/bin/gh")
    def test_github_text_uses_authenticated_gh_api(self, which, run) -> None:
        run.return_value = subprocess.CompletedProcess(["gh"], 0, "name: Lucy\n", "")

        text = _github_text("lucy", "src/lucy/identity.yaml")

        self.assertEqual(text, "name: Lucy\n")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/local/bin/gh", "api", "-H", "Accept: application/vnd.github.raw"])
        self.assertEqual(command[-1], "repos/our-ark/lucy/contents/src/lucy/identity.yaml?ref=main")


if __name__ == "__main__":
    unittest.main()
