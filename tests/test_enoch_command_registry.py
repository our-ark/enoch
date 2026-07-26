import unittest

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.commands import CORE_COMMANDS, core_command, core_command_names, help_message


class CoreCommandRegistryTests(unittest.TestCase):
    def test_registry_is_the_source_for_command_names_and_help(self) -> None:
        names = tuple(command.name for command in CORE_COMMANDS)

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(core_command_names(), frozenset(names))

        overview = help_message()
        for command in CORE_COMMANDS:
            with self.subTest(command=command.name):
                self.assertIs(core_command(command.name), command)
                self.assertIs(core_command(command.command), command)
                self.assertIn(command.summary_line(), overview)
                self.assertEqual(
                    help_message(command.name),
                    command.usage_message(),
                )

    def test_removed_aliases_and_stale_thinking_help_are_not_registered(self) -> None:
        for name in (
            "tasks",
            "backlogs",
            "crons",
            "worktrees",
            "thinking",
            "feedback",
            "experience",
            "propose",
        ):
            with self.subTest(command=name):
                self.assertIsNone(core_command(name))
                self.assertIn(
                    f"No help found for /{name}.",
                    help_message(name),
                )

    def test_help_overview_prominently_explains_detailed_help(self) -> None:
        overview = help_message()

        self.assertIn(
            "Use /help <command> for detailed usage and subcommands.",
            overview,
        )
        self.assertIn("Example: /help worktree", overview)

    def test_evolve_help_exposes_only_the_consolidated_surface(self) -> None:
        usage = help_message("evolve")

        self.assertIn("/evolve evidence [feedback|experience|all]", usage)
        self.assertIn("/evolve scan [feedback|experience|all]", usage)
        self.assertIn("/evolve candidates [all]", usage)
        self.assertIn("/evolve propose", usage)
        self.assertIn("/evolve config feedback-batch <1-100>", usage)
        self.assertNotIn("/evolve list", usage)
        self.assertNotIn("/evolve mode", usage)


if __name__ == "__main__":
    unittest.main()
