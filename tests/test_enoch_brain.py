from pathlib import Path
import json
import sys
import threading
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.brain import (
    BrainCancelled,
    CodexAccessUnavailable,
    act,
    act_in_session,
    model_summary,
    respond,
)
from enoch import brain
from enoch.codex_sessions import CodexSessionState, codex_sessions_path, load_codex_session, save_codex_session
from enoch.identity import load_identity
from enoch.last_codex_input import last_codex_input_message
from enoch.memory.store import remember_memory


class EnochBrainTests(unittest.TestCase):
    def test_default_progress_interval_is_one_minute(self) -> None:
        self.assertEqual(brain.DEFAULT_PROGRESS_INTERVAL_SECONDS, 60)

    @patch.dict("os.environ", {"ENOCH_CODEX_MODEL": "gpt-5-codex"}, clear=True)
    def test_model_summary_reports_configured_model(self) -> None:
        summary = model_summary()

        self.assertIn("AI model: gpt-5-codex", summary)
        self.assertIn("Model source: ENOCH_CODEX_MODEL", summary)

    @patch.dict("os.environ", {}, clear=True)
    def test_model_summary_reports_codex_default(self) -> None:
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"CODEX_HOME": temp}, clear=True):
                summary = model_summary()

        self.assertIn("AI model: Codex CLI default", summary)
        self.assertIn("Enoch config codex.model", summary)
        self.assertIn("Codex config model are not set", summary)

    def test_model_summary_reports_codex_config_model_and_reasoning(self) -> None:
        with TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text(
                "\n".join(
                    [
                        'model = "gpt-5.5"',
                        'model_reasoning_effort = "high"',
                        "",
                        "[projects]",
                        'ignored = "value"',
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CODEX_HOME": temp}, clear=True):
                summary = model_summary()

        self.assertIn("AI model: gpt-5.5", summary)
        self.assertIn(f"Model source: {config}", summary)
        self.assertIn("Reasoning effort: high", summary)

    def test_model_summary_keeps_reasoning_when_model_env_overrides_config(self) -> None:
        with TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text(
                "\n".join(
                    [
                        'model = "gpt-5.5"',
                        'model_reasoning_effort = "medium"',
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"CODEX_HOME": temp, "ENOCH_CODEX_MODEL": "gpt-5-codex"},
                clear=True,
            ):
                summary = model_summary()

        self.assertIn("AI model: gpt-5-codex", summary)
        self.assertIn("Model source: ENOCH_CODEX_MODEL", summary)
        self.assertIn("Reasoning effort: medium", summary)
        self.assertIn("Codex config model: gpt-5.5", summary)

    def test_model_summary_prefers_enoch_config_over_codex_config_per_setting(self) -> None:
        with TemporaryDirectory() as temp, TemporaryDirectory() as codex_home:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "codex:",
                        "  model: gpt-enoch-local",
                        "  reasoning_effort: high",
                    ]
                ),
                encoding="utf-8",
            )
            (Path(codex_home) / "config.toml").write_text(
                "\n".join(
                    [
                        'model = "gpt-global"',
                        'model_reasoning_effort = "low"',
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CODEX_HOME": codex_home}, clear=True):
                summary = model_summary(root)

        self.assertIn("AI model: gpt-enoch-local", summary)
        self.assertIn("Model source: Enoch config codex.model", summary)
        self.assertIn("Reasoning effort: high", summary)
        self.assertIn("Reasoning source: Enoch config codex.reasoning_effort", summary)

    def test_model_summary_reports_enoch_reasoning_override(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("\n".join(["codex:", "  reasoning_effort: high"]), encoding="utf-8")

            summary = model_summary(root)

        self.assertIn("Reasoning effort: high", summary)
        self.assertIn("Reasoning source: Enoch config codex.reasoning_effort", summary)

    @patch("enoch.brain._codex_binary", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_codex_model_options_reads_visible_installed_catalog(
        self,
        run: MagicMock,
        _codex_binary: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "description": "Latest frontier model.",
                        "visibility": "list",
                    },
                    {
                        "slug": "hidden-model",
                        "display_name": "Hidden",
                        "visibility": "hidden",
                    },
                ]
            }
        )

        options = brain.codex_model_options()

        self.assertEqual(
            options,
            (
                brain.CodexModelOption(
                    slug="gpt-5.6-sol",
                    display_name="GPT-5.6-Sol",
                    description="Latest frontier model.",
                ),
            ),
        )
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/local/bin/codex", "debug", "models", "--bundled"],
        )

    def test_model_summary_reports_enoch_model_override(self) -> None:
        with TemporaryDirectory() as temp, TemporaryDirectory() as codex_home:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(["codex:", "  model: gpt-enoch-local"]),
                encoding="utf-8",
            )
            (Path(codex_home) / "config.toml").write_text(
                'model = "gpt-global"\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CODEX_HOME": codex_home}, clear=True):
                summary = model_summary(root)

        self.assertIn("AI model: gpt-enoch-local", summary)
        self.assertIn("Model source: Enoch config codex.model", summary)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_responds_through_codex_exec(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        identity = load_identity()
        with TemporaryDirectory() as temp:
            answer = respond(identity, "Who are you?", cwd=Path(temp))

        self.assertEqual(answer, "")
        args = run.call_args.args[0]
        prompt = run.call_args.kwargs["input"]

        self.assertEqual(args[:2], ["/usr/local/bin/codex", "exec"])
        self.assertIn("--sandbox", args)
        self.assertIn("read-only", args)
        self.assertIn("--json", args)
        self.assertIn("--ephemeral", args)
        self.assertEqual(prompt, "Human message:\nWho are you?")
        self.assertIn("Who are you?", prompt)
        self.assertNotIn("Branch: main", prompt)
        self.assertNotIn("Memory context:", prompt)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_missing_codex_login_raises_recoverable_access_error(
        self,
        run: MagicMock,
        _which: MagicMock,
    ) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "Not logged in. Run codex login to continue."

        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_codex_session(
                CodexSessionState(
                    key="telegram:42",
                    session_id="session-123",
                    turn_count=3,
                    created_at="2026-06-18T00:00:00+00:00",
                    updated_at="2026-06-18T00:00:00+00:00",
                ),
                root,
            )
            with self.assertRaisesRegex(
                CodexAccessUnavailable,
                "authentication is unavailable",
            ):
                respond(
                    load_identity(),
                    "Hello",
                    cwd=root,
                    session_key="telegram:42",
                )
            state = load_codex_session("telegram:42", root)

        self.assertEqual(run.call_count, 1)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.session_id, "session-123")

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_codex_quota_exhaustion_raises_recoverable_access_error(
        self,
        run: MagicMock,
        _which: MagicMock,
    ) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = '{"error":{"code":"insufficient_quota"}}'
        run.return_value.stderr = ""

        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(
                CodexAccessUnavailable,
                "quota is currently unavailable",
            ):
                respond(load_identity(), "Hello", cwd=Path(temp))

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_respond_passes_enoch_reasoning_effort_to_codex(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("\n".join(["codex:", "  reasoning_effort: medium"]), encoding="utf-8")

            respond(load_identity(), "Hello", cwd=root)

        args = run.call_args.args[0]
        self.assertIn("-c", args)
        self.assertIn('model_reasoning_effort="medium"', args)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_respond_passes_enoch_model_to_codex(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(["codex:", "  model: gpt-enoch-local"]),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                respond(load_identity(), "Hello", cwd=root)

        args = run.call_args.args[0]
        self.assertIn("--model", args)
        self.assertEqual(args[args.index("--model") + 1], "gpt-enoch-local")

    @patch.dict("os.environ", {}, clear=True)
    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_respond_uses_configured_task_timeout(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("task:\n  timeout_seconds: 1800\n", encoding="utf-8")

            respond(load_identity(), "Hello", cwd=root)

        self.assertEqual(run.call_args.kwargs["timeout"], 1800)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_respond_records_last_completed_turn_token_usage(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "\n".join(
            [
                '{"type":"item.completed","usage":{"input_tokens":999}}',
                '{"type":"turn.completed","usage":{"input_tokens":123,"cached_input_tokens":100,"output_tokens":45}}',
                '{"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":3,"output_tokens":2,"reasoning_output_tokens":1}}',
            ]
        )
        run.return_value.stderr = ""
        brain.reset_token_usage()

        with TemporaryDirectory() as temp:
            respond(load_identity(), "Hello", cwd=Path(temp))

        usage = brain.token_usage()
        self.assertEqual(usage.input_tokens, 7)
        self.assertEqual(usage.cached_input_tokens, 3)
        self.assertEqual(usage.uncached_input_tokens, 4)
        self.assertEqual(usage.output_tokens, 2)
        self.assertEqual(usage.reasoning_output_tokens, 1)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.memory_for_prompt", return_value="Identity and long-term memory.")
    @patch("enoch.brain.subprocess.run")
    def test_respond_starts_persistent_codex_session(
        self, run: MagicMock, _memory: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"session-123"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Hello Roy"}}',
            ]
        )
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)

            answer = respond(load_identity(), "hello", cwd=root, session_key="telegram:42")
            state = load_codex_session("telegram:42", root)
            last_input = last_codex_input_message(root)

        args = run.call_args.args[0]
        prompt = run.call_args.kwargs["input"]
        self.assertEqual(answer, "Hello Roy")
        self.assertNotIn("--ephemeral", args)
        self.assertIn("Enoch startup context:", prompt)
        self.assertIn("Identity and long-term memory.", prompt)
        self.assertIn("Human message:\n\nhello", prompt)
        _memory.assert_called_once_with(root)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.session_id, "session-123")
        self.assertEqual(state.turn_count, 1)
        self.assertIn("Human message:\n\nhello", last_input)
        self.assertIn("persistent session: yes", last_input)
        self.assertIn("resumed session: no", last_input)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_respond_resumes_persistent_codex_session_without_periodic_memory_sync(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"Still here"}}'
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_codex_session(
                CodexSessionState(
                    key="telegram:42",
                    session_id="session-123",
                    turn_count=9,
                    created_at="2026-06-18T00:00:00+00:00",
                    updated_at="2026-06-18T00:00:00+00:00",
                ),
                root,
            )

            answer = respond(load_identity(), "the tenth turn", cwd=root, session_key="telegram:42")
            state = load_codex_session("telegram:42", root)
            last_input = last_codex_input_message(root)

        args = run.call_args.args[0]
        prompt = run.call_args.kwargs["input"]
        self.assertEqual(args[:4], ["/usr/local/bin/codex", "exec", "resume", "session-123"])
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertEqual(prompt, "Human message:\nthe tenth turn")
        self.assertEqual(answer, "Still here")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.turn_count, 10)
        self.assertNotIn("Persistent Enoch context sync:", last_input)
        self.assertIn("resumed session: yes", last_input)
        self.assertIn("session id: session-123", last_input)

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.memory_for_prompt", return_value="Old memory should not be sent.")
    @patch("enoch.brain.subprocess.run")
    def test_respond_ignores_old_persistent_codex_session_prompt_versions(
        self, run: MagicMock, _memory: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"session-new"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Fresh session"}}',
            ]
        )
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = codex_sessions_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"schema_version":1,"sessions":{"telegram:42":{"session_id":"session-old","turn_count":99}}}',
                encoding="utf-8",
            )

            answer = respond(load_identity(), "hello", cwd=root, session_key="telegram:42")
            state = load_codex_session("telegram:42", root)

        args = run.call_args.args[0]
        prompt = run.call_args.kwargs["input"]
        self.assertNotIn("resume", args)
        self.assertIn("Enoch startup context:", prompt)
        self.assertIn("Old memory should not be sent.", prompt)
        self.assertIn("Human message:\n\nhello", prompt)
        _memory.assert_called_once_with(root)
        self.assertEqual(answer, "Fresh session")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.session_id, "session-new")

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_act_in_session_resumes_with_action_sandbox(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"Edited files"}}'
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_codex_session(
                CodexSessionState(
                    key="telegram:42",
                    session_id="session-123",
                    turn_count=1,
                    created_at="2026-06-18T00:00:00+00:00",
                    updated_at="2026-06-18T00:00:00+00:00",
                ),
                root,
            )

            answer = act_in_session(
                load_identity(),
                "Implement the requested edit.",
                cwd=root,
                sandbox="danger-full-access",
                session_key="telegram:42",
            )
            state = load_codex_session("telegram:42", root)

        args = run.call_args.args[0]
        prompt = run.call_args.kwargs["input"]
        self.assertEqual(answer, "Edited files")
        self.assertEqual(args[:4], ["/usr/local/bin/codex", "exec", "resume", "session-123"])
        self.assertIn('sandbox_mode="danger-full-access"', args)
        self.assertEqual(prompt, "Human message:\nImplement the requested edit.")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.turn_count, 2)

    @patch.dict("os.environ", {}, clear=True)
    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_act_in_session_separates_working_directory_from_agent_state(
        self,
        run: MagicMock,
        _which: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"session-task"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Edited task worktree"}}',
            ]
        )
        run.return_value.stderr = ""
        with TemporaryDirectory() as temp:
            base = Path(temp)
            state_root = base / "resident"
            work_root = base / "task-worktree"
            state_root.mkdir()
            work_root.mkdir()
            config = state_root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                'codex:\n  model: "gpt-5.6-sol"\n  reasoning_effort: "high"\n',
                encoding="utf-8",
            )
            remember_memory("Keep task state in the resident worktree.", root=state_root)

            answer = act_in_session(
                load_identity(),
                "Implement isolated work.",
                cwd=work_root,
                state_root=state_root,
                sandbox="danger-full-access",
                session_key="telegram:42:task:1",
            )

            state = load_codex_session("telegram:42:task:1", state_root)
            args = run.call_args.args[0]
            prompt = run.call_args.kwargs["input"]

        self.assertEqual(answer, "Edited task worktree")
        self.assertEqual(args[args.index("--cd") + 1], str(work_root))
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIn("Keep task state in the resident worktree.", prompt)
        self.assertIsNotNone(state)
        self.assertFalse((work_root / ".enoch").exists())

    @patch(
        "enoch.brain._run_codex_result",
        side_effect=BrainCancelled("Enoch cancelled the active Codex run."),
    )
    def test_act_in_session_does_not_retry_cancelled_session(
        self, run_codex: MagicMock
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            save_codex_session(
                CodexSessionState(
                    key="telegram:42",
                    session_id="session-123",
                    turn_count=1,
                    created_at="2026-06-18T00:00:00+00:00",
                    updated_at="2026-06-18T00:00:00+00:00",
                ),
                root,
            )

            with self.assertRaises(BrainCancelled):
                act_in_session(
                    load_identity(),
                    "Implement the requested edit.",
                    cwd=root,
                    session_key="telegram:42",
                    cancellation_event=threading.Event(),
                )

        run_codex.assert_called_once()

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_act_uses_workspace_write_sandbox(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        identity = load_identity()
        with TemporaryDirectory() as temp:
            act(identity, "Add a branch command.", cwd=Path(temp))

        args = run.call_args.args[0]
        prompt = run.call_args.kwargs["input"]

        self.assertIn("--sandbox", args)
        self.assertIn("workspace-write", args)
        self.assertEqual(prompt, "Human message:\nAdd a branch command.")

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    @patch("enoch.brain.subprocess.run")
    def test_act_accepts_configured_sandbox(
        self, run: MagicMock, _which: MagicMock
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        with TemporaryDirectory() as temp:
            act(load_identity(), "Do the task.", cwd=Path(temp), sandbox="danger-full-access")

        args = run.call_args.args[0]
        self.assertIn("--sandbox", args)
        self.assertIn("danger-full-access", args)

    @patch.dict("os.environ", {}, clear=True)
    @patch("enoch.brain.os.access", return_value=True)
    @patch("enoch.brain.Path.is_file", return_value=True)
    @patch("enoch.brain.shutil.which", return_value=None)
    def test_falls_back_to_default_codex_app_path(
        self,
        _which: MagicMock,
        _is_file: MagicMock,
        _access: MagicMock,
    ) -> None:
        resolution = brain.resolve_codex_executable()

        self.assertEqual(
            resolution.path,
            "/Applications/ChatGPT.app/Contents/Resources/codex",
        )
        self.assertIn("known macOS path", resolution.source)

    @patch.dict("os.environ", {}, clear=True)
    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    def test_enoch_config_codex_executable_precedes_path(self, _which: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            configured = root / "Codex Runtime" / "codex"
            configured.parent.mkdir()
            configured.write_text("#!/bin/sh\n", encoding="utf-8")
            configured.chmod(0o755)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                f'codex:\n  executable: "{configured}"\n',
                encoding="utf-8",
            )

            resolution = brain.resolve_codex_executable(root)

        self.assertEqual(resolution.path, str(configured))
        self.assertEqual(resolution.source, "Enoch config codex.executable")

    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    def test_environment_codex_executable_precedes_enoch_config(self, _which: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            configured = root / "config-codex"
            configured.write_text("#!/bin/sh\n", encoding="utf-8")
            configured.chmod(0o755)
            environment = root / "env-codex"
            environment.write_text("#!/bin/sh\n", encoding="utf-8")
            environment.chmod(0o755)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                f'codex:\n  executable: "{configured}"\n',
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"ENOCH_CODEX_BIN": str(environment)},
                clear=True,
            ):
                resolution = brain.resolve_codex_executable(root)

        self.assertEqual(resolution.path, str(environment))
        self.assertEqual(resolution.source, "ENOCH_CODEX_BIN")

    @patch.dict("os.environ", {}, clear=True)
    @patch("enoch.brain.shutil.which", return_value="/usr/local/bin/codex")
    def test_invalid_explicit_codex_executable_does_not_silently_fall_back(
        self,
        _which: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                'codex:\n  executable: "/missing/codex"\n',
                encoding="utf-8",
            )

            resolution = brain.resolve_codex_executable(root)

        self.assertIsNone(resolution.path)
        self.assertEqual(resolution.source, "Enoch config codex.executable")
        self.assertIn("does not exist or is not executable", resolution.detail)

    @patch("enoch.brain.time.sleep", side_effect=KeyboardInterrupt)
    @patch("enoch.brain.time.monotonic", side_effect=[0, 1])
    @patch("enoch.brain.tempfile.TemporaryFile")
    @patch("enoch.brain.subprocess.Popen")
    def test_progress_run_can_be_cancelled(
        self,
        popen: MagicMock,
        temporary_file: MagicMock,
        _monotonic: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        stdout_file = MagicMock()
        stderr_file = MagicMock()
        temporary_file.return_value.__enter__.side_effect = [stdout_file, stderr_file]
        process = popen.return_value
        process.poll.return_value = None
        process.stdin.write.return_value = None
        process.stdin.close.return_value = None

        with self.assertRaises(BrainCancelled):
            brain._run_with_progress(
                args=["codex", "exec"],
                prompt="do work",
                timeout=60,
                sandbox="workspace-write",
                progress_callback=lambda _elapsed, _sandbox: None,
            )

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    @patch("enoch.brain.time.sleep")
    @patch("enoch.brain.time.monotonic", side_effect=[0, 1])
    @patch("enoch.brain.tempfile.TemporaryFile")
    @patch("enoch.brain.subprocess.Popen")
    def test_progress_run_stops_when_cancellation_event_is_set(
        self,
        popen: MagicMock,
        temporary_file: MagicMock,
        _monotonic: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        stdout_file = MagicMock()
        stderr_file = MagicMock()
        temporary_file.return_value.__enter__.side_effect = [stdout_file, stderr_file]
        process = popen.return_value
        process.poll.return_value = None
        process.stdin.write.return_value = None
        process.stdin.close.return_value = None
        cancellation_event = threading.Event()
        cancellation_event.set()

        with self.assertRaises(BrainCancelled):
            brain._run_with_progress(
                args=["codex", "exec"],
                prompt="do work",
                timeout=60,
                sandbox="workspace-write",
                progress_callback=lambda _elapsed, _sandbox: None,
                cancellation_event=cancellation_event,
            )

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
