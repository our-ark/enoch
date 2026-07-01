from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.config import read_section, write_section_value
from enoch.memory.config import DEFAULT_MEMORY_SETTINGS, memory_settings


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

if __name__ == "__main__":
    unittest.main()
