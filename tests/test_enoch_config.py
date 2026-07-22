from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.config import read_section, write_section_value
from enoch.memory.config import DEFAULT_MEMORY_SETTINGS, memory_settings
from enoch.tasks.config import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    parse_task_timeout,
    save_task_timeout,
    task_settings,
)


class EnochConfigTests(unittest.TestCase):
    def test_write_section_value_adds_new_section(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            write_section_value("codex", "reasoning_effort", "high", root)

            self.assertEqual(read_section("codex", root)["reasoning_effort"], "high")

    def test_write_section_value_updates_existing_key_and_preserves_other_keys(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "telegram:",
                        "  allowed_chat_id: 42",
                        "codex:",
                        "  model: gpt-5-codex",
                        "  reasoning_effort: low",
                    ]
                ),
                encoding="utf-8",
            )

            write_section_value("codex", "reasoning_effort", "medium", root)

            self.assertEqual(read_section("telegram", root)["allowed_chat_id"], "42")
            self.assertEqual(read_section("codex", root)["model"], "gpt-5-codex")
            self.assertEqual(read_section("codex", root)["reasoning_effort"], "medium")

    def test_write_section_value_removes_key_for_default(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(["codex:", "  model: gpt-5-codex", "  reasoning_effort: high"]),
                encoding="utf-8",
            )

            write_section_value("codex", "reasoning_effort", None, root)

            self.assertNotIn("reasoning_effort", read_section("codex", root))
            self.assertEqual(read_section("codex", root)["model"], "gpt-5-codex")

    def test_memory_settings_use_defaults_and_config_overrides(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "memory:",
                        "  long_term_prompt_max_chars: 12000",
                        "  identity_prompt_max_chars: 6000",
                        "  long_term_memory_text_max_chars: 300",
                        "  long_term_memory_subject_max_chars: 40",
                    ]
                ),
                encoding="utf-8",
            )

            settings = memory_settings(root)

        self.assertEqual(settings.long_term_prompt_max_chars, 12000)
        self.assertEqual(settings.identity_prompt_max_chars, 6000)
        self.assertEqual(settings.long_term_memory_text_max_chars, 300)
        self.assertEqual(settings.long_term_memory_subject_max_chars, 40)
        self.assertEqual(
            DEFAULT_MEMORY_SETTINGS.long_term_memory_text_max_chars,
            500,
        )

    def test_task_timeout_defaults_to_ten_minutes(self) -> None:
        with TemporaryDirectory() as temp:
            settings = task_settings(Path(temp))

        self.assertEqual(settings.timeout_seconds, DEFAULT_TASK_TIMEOUT_SECONDS)
        self.assertTrue(settings.uses_default_timeout)

    def test_task_timeout_can_be_configured_and_reset(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            configured = save_task_timeout(parse_task_timeout("30m"), root)
            self.assertEqual(read_section("task", root)["timeout_seconds"], "1800")
            reset = save_task_timeout(None, root)

        self.assertEqual(configured.timeout_seconds, 1800)
        self.assertFalse(configured.uses_default_timeout)
        self.assertEqual(reset.timeout_seconds, DEFAULT_TASK_TIMEOUT_SECONDS)
        self.assertTrue(reset.uses_default_timeout)

    def test_invalid_task_timeout_config_falls_back_to_default(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_section_value("task", "timeout_seconds", "invalid", root)

            settings = task_settings(root)

        self.assertEqual(settings.timeout_seconds, DEFAULT_TASK_TIMEOUT_SECONDS)
        self.assertTrue(settings.uses_default_timeout)

    def test_task_timeout_parser_enforces_bounds(self) -> None:
        self.assertEqual(parse_task_timeout("1m"), 60)
        self.assertEqual(parse_task_timeout("2h"), 7200)
        with self.assertRaises(ValueError):
            parse_task_timeout("59s")
        with self.assertRaises(ValueError):
            parse_task_timeout("3h")
        with self.assertRaises(ValueError):
            parse_task_timeout("off")

if __name__ == "__main__":
    unittest.main()
