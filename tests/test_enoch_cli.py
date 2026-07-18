from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.cli import _repl, main
from enoch.config import read_section
from enoch.immune import DoctorCheckResult, DoctorDiagnosis
from enoch.identity import load_identity


class EnochCliTests(unittest.TestCase):
    def test_help_shows_admin_command_surface(self) -> None:
        output = _run_repl_commands("help", "exit")

        self.assertIn("status      Show identity, model, and lineage.", output)
        self.assertIn("init        Create or claim a local Enoch instance worktree.", output)
        self.assertIn("setup       Configure Telegram token, chat lock, and setup status.", output)
        self.assertIn("thinking    Show or set Enoch's Codex thinking level.", output)
        self.assertIn("mission     Show or update Enoch's mission.", output)
        self.assertNotIn("memory      Manage long-term memory", output)
        self.assertIn("ancestors   Inspect ancestor chain and inheritable updates.", output)
        self.assertIn("inherit     Show inheritable direct-parent changes.", output)
        self.assertIn("skills      Show declared skills for Enoch or another local agent.", output)
        self.assertNotIn("teach       Package local changes as a portable lesson.", output)
        self.assertIn("learn       Inspect a published skill for adaptation.", output)
        self.assertNotIn("debug       Inspect prompts, logs, state files, and worktree health.", output)
        self.assertNotIn("mode        Show or set chat/work mode.", output)
        self.assertIn("doctor      Run Enoch's local health checks", output)
        self.assertIn("update      Pull latest main, run doctor, and restart Enoch if safe.", output)
        self.assertIn("Enoch CLI is admin-only.", output)
        self.assertNotIn("lastinput   Shortcut", output)
        self.assertNotIn("checktree   Shortcut", output)
        self.assertNotIn("Natural input is classified", output)

    @patch("enoch.cli.model_summary", return_value="AI model: gpt-5-codex")
    def test_status_combines_identity_model_and_lineage(self, model_summary: MagicMock) -> None:
        output = _run_repl_commands("status", "exit")

        model_summary.assert_called_once_with(ROOT)
        self.assertIn("Enoch status:", output)
        self.assertIn("I am Enoch.", output)
        self.assertIn("Mission:", output)
        self.assertIn("AI model: gpt-5-codex", output)
        self.assertNotIn("action mode", output)

    def test_init_claims_current_worktree_as_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            output = _run_repl_commands("init --instance gary", "exit", root=root)
            metadata = root / ".agent" / "instance.yaml"

            self.assertTrue(metadata.exists())
            text = metadata.read_text(encoding="utf-8")
            self.assertIn('name: "gary"', text)
            self.assertIn('package: "enoch"', text)
            self.assertIn(f'path: "{root.resolve()}"', text)
            self.assertIn("Initialized current worktree instance for Enoch.", output)
            self.assertIn(f"Metadata: {metadata.resolve()}", output)

    def test_init_can_create_linked_git_worktree_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "enoch"
            instance = Path(directory) / "instances" / "gary-enoch"
            root.mkdir()
            _git(root, "init")
            (root / "README.md").write_text("Enoch\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "seed")

            output = _run_repl_commands(
                f"init --instance gary --worktree {instance}",
                "exit",
                root=root,
            )
            metadata = instance / ".agent" / "instance.yaml"

            self.assertTrue(metadata.exists())
            self.assertTrue((instance / ".git").exists())
            text = metadata.read_text(encoding="utf-8")
            self.assertIn('name: "gary"', text)
            self.assertIn(f'path: "{instance.resolve()}"', text)
            self.assertIn('branch: "agent/enoch-gary"', text)
            self.assertIn("Created worktree instance for Enoch.", output)
            self.assertIn(f"Worktree: {instance.resolve()}", output)

    @patch("enoch.cli.model_summary", return_value="AI model: gpt-5-codex")
    def test_status_uses_lineage_parent_before_identity_ancestor(self, model_summary: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lineage = root / ".agent" / "lineage.yaml"
            lineage.parent.mkdir()
            lineage.write_text(
                "\n".join(["parent:", "  name: Adam", "  repo: our-ark/adam"]),
                encoding="utf-8",
            )

            output = _run_repl_commands("status", "exit", root=root)

        self.assertIn("Ancestor: Adam", output)
        self.assertNotIn("Ancestor: Lucy", output)

    @patch("enoch.cli.mission_command", return_value="Mission: ok")
    def test_mission_command_uses_shared_command(self, mission_command: MagicMock) -> None:
        output = _run_repl_commands("mission", "exit")

        mission_command.assert_called_once()
        self.assertEqual(mission_command.call_args.args[0], "mission")
        self.assertEqual(mission_command.call_args.args[2], ROOT)
        self.assertEqual(mission_command.call_args.kwargs, {"prefix": ""})
        self.assertIn("Mission: ok", output)

    @patch("enoch.cli.lineage_command", return_value="Ancestors: no direct parent configured.")
    def test_ancestors_command_uses_shared_command(self, lineage_command: MagicMock) -> None:
        output = _run_repl_commands("ancestors", "exit")

        lineage_command.assert_called_once_with("ancestors", ROOT, prefix="", command_name="ancestors")
        self.assertIn("Ancestors: no direct parent configured.", output)

    @patch("enoch.cli.skills_command", return_value="Enoch skills:")
    def test_skills_command_uses_shared_command(self, skills_command: MagicMock) -> None:
        output = _run_repl_commands("skills lucy", "exit")

        skills_command.assert_called_once_with("skills lucy", ROOT, prefix="")
        self.assertIn("Enoch skills:", output)

    def test_teach_command_is_not_user_facing(self) -> None:
        output = _run_repl_commands("teach natural agency", "exit")

        self.assertIn("Enoch CLI is admin-only now.", output)
        self.assertNotIn("Enoch created a lesson.", output)

    @patch("enoch.cli.learn_command", return_value="Enoch inspected Lucy's teach skill.")
    def test_learn_command_uses_shared_command(self, learn_command: MagicMock) -> None:
        output = _run_repl_commands("learn teach from lucy", "exit")

        learn_command.assert_called_once_with("learn teach from lucy", ROOT, prefix="")
        self.assertIn("Enoch inspected Lucy's teach skill.", output)

    @patch("enoch.cli._schedule_daemon_restart")
    @patch("enoch.cli._record_direct_action")
    @patch("enoch.cli.update_from_main")
    def test_update_records_and_restarts_from_shared_result(
        self,
        update_from_main: MagicMock,
        record_direct_action: MagicMock,
        schedule_restart: MagicMock,
    ) -> None:
        update_from_main.return_value.message = "Enoch pulled latest main and doctor passed."
        update_from_main.return_value.direct_action_result = "Updating 1111111..2222222"
        update_from_main.return_value.restart_required = True

        output = _run_repl_commands("update", "exit")

        update_from_main.assert_called_once_with(ROOT)
        record_direct_action.assert_called_once_with("update from main", "Updating 1111111..2222222", ROOT)
        schedule_restart.assert_called_once_with(ROOT)
        self.assertIn("Enoch pulled latest main and doctor passed.", output)

    def test_unknown_input_is_admin_only_message(self) -> None:
        output = _run_repl_commands("add test4 to README", "commit this", "exit")

        self.assertEqual(output.count("Enoch CLI is admin-only now."), 2)
        self.assertIn("Use Telegram for conversation, repository edits, and self-evolution.", output)
        self.assertNotIn("Input tokens:", output)

    def test_setup_interactively_saves_token_without_printing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            output = _run_repl_commands("setup", "123456:secret-token", "exit", root=root)

            self.assertEqual(read_section("telegram", root)["bot_token"], "123456:secret-token")
            self.assertIn("Telegram bot token saved", output)
            self.assertIn("bin/enoch-daemon start", output)
            self.assertEqual(output.count("Next:"), 1)
            self.assertNotIn("123456:secret-token", output)

    def test_setup_token_shortcut_shows_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = StringIO()

            with patch("enoch.cli.Path.cwd", return_value=root), redirect_stdout(output):
                main(["setup-token", "123456:secret-token"])

            self.assertEqual(read_section("telegram", root)["bot_token"], "123456:secret-token")
            self.assertIn("Telegram bot token saved", output.getvalue())
            self.assertIn("Next:", output.getvalue())
            self.assertIn("bin/enoch-daemon start", output.getvalue())
            self.assertNotIn("123456:secret-token", output.getvalue())

    def test_setup_chat_shortcut_saves_chat_lock_from_direct_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = StringIO()

            with patch("enoch.cli.Path.cwd", return_value=root), redirect_stdout(output):
                main(["setup-chat", "42"])

            self.assertEqual(read_section("telegram", root)["allowed_chat_id"], "42")
            self.assertIn("Telegram chat lock saved", output.getvalue())
            self.assertIn("bin/enoch-daemon restart", output.getvalue())

    def test_setup_ancestor_writes_repo_side_lineage_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            output = _run_repl_commands(
                "setup ancestor https://github.com/our-ark/enoch",
                "setup ancestor show",
                "exit",
                root=root,
            )

            lineage = root / ".agent" / "lineage.yaml"
            self.assertIn("Lineage parent saved", output)
            self.assertIn("Parent: Enoch (our-ark/enoch@main)", output)
            self.assertIn("Lineage parent: Enoch (our-ark/enoch@main)", output)
            self.assertIn("repo-side lineage metadata", output)
            self.assertIn("name: Enoch", lineage.read_text(encoding="utf-8"))
            self.assertIn("repo: our-ark/enoch", lineage.read_text(encoding="utf-8"))
            self.assertIn("branch: main", lineage.read_text(encoding="utf-8"))

    def test_setup_ancestor_rejects_non_link_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            output = _run_repl_commands(
                "setup ancestor our-ark/research-agent",
                "setup ancestor https://github.com/our-ark/enoch dev",
                "setup ancestor https://github.com/our-ark/enoch --name Enoch",
                "exit",
                root=root,
            )

            self.assertFalse((root / ".agent" / "lineage.yaml").exists())
            self.assertEqual(output.count("Use bin/enoch setup ancestor <github-url>."), 3)

    def test_setup_ancestor_clear_removes_lineage_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            output = _run_repl_commands(
                "setup ancestor https://github.com/our-ark/enoch",
                "setup ancestor clear",
                "setup ancestor show",
                "exit",
                root=root,
            )

            self.assertFalse((root / ".agent" / "lineage.yaml").exists())
            self.assertIn("Removed lineage parent", output)
            self.assertIn("No lineage parent configured", output)

    @patch("enoch.cli.model_summary", return_value="AI model: gpt-5-codex")
    def test_thinking_command_uses_cli_command_names(self, model_summary: MagicMock) -> None:
        output = _run_repl_commands("thinking", "exit")

        model_summary.assert_called_once_with(ROOT)
        self.assertIn("Enoch thinking status:", output)
        self.assertIn("Set with thinking low, thinking medium, thinking high, or thinking default.", output)
        self.assertNotIn("/thinking low", output)

    @patch("enoch.cli.run_immune_system")
    def test_doctor_runs_local_health_checks(self, run_immune_system: MagicMock) -> None:
        run_immune_system.return_value.command = "python3 -m unittest"
        run_immune_system.return_value.passed = True
        run_immune_system.return_value.output = "ok"
        run_immune_system.return_value.checks = [
            DoctorCheckResult(
                name="tests",
                passed=True,
                command="python3 -m unittest",
                output="ok",
                category="code health",
                summary="OK",
            )
        ]
        run_immune_system.return_value.diagnosis = DoctorDiagnosis(
            summary="All configured health checks passed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="No repair needed.",
        )

        output = _run_repl_commands("doctor", "exit")

        run_immune_system.assert_called_once_with(ROOT)
        self.assertIn("Doctor passed.", output)
        self.assertIn("Code health:", output)
        self.assertIn("- tests: passed (OK)", output)
        self.assertIn("Diagnosis: All configured health checks passed.", output)
        self.assertIn("Suggested next action: No repair needed.", output)

    @patch("enoch.cli.run_immune_system")
    def test_doctor_prints_failure_diagnosis(self, run_immune_system: MagicMock) -> None:
        run_immune_system.return_value.command = "python3 -m unittest"
        run_immune_system.return_value.passed = False
        run_immune_system.return_value.output = "FAILED (failures=1)"
        run_immune_system.return_value.checks = [
            DoctorCheckResult(
                name="tests",
                passed=False,
                command="python3 -m unittest",
                output="FAILED (failures=1)",
                category="code health",
                summary="FAILED (failures=1)",
            )
        ]
        run_immune_system.return_value.diagnosis = DoctorDiagnosis(
            summary="1 test(s) failed.",
            failing_tests=["tests.test_enoch_cli.EnochCliTests.test_help"],
            likely_files=["tests/test_enoch_cli.py"],
            suggested_action="Inspect the failing tests, make one focused repair pass, then run doctor again.",
        )

        output = _run_repl_commands("doctor", "exit")

        self.assertIn("Doctor failed.", output)
        self.assertIn("- tests: failed (FAILED (failures=1))", output)
        self.assertIn("Diagnosis: 1 test(s) failed.", output)
        self.assertIn("- tests.test_enoch_cli.EnochCliTests.test_help", output)
        self.assertIn("- tests/test_enoch_cli.py", output)
        self.assertIn("Failed check: tests", output)
        self.assertIn("Check output:", output)


def _run_repl_commands(*commands: str, root: Path = ROOT) -> str:
    identity = load_identity()
    output = StringIO()
    with patch("builtins.input", side_effect=commands), redirect_stdout(output):
        _repl(identity, root)
    return output.getvalue()


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Enoch Test",
            "-c",
            "user.email=enoch-test@example.com",
            *args,
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
