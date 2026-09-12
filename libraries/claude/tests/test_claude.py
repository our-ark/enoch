from pathlib import Path
import json
import os
import sys
import threading
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "claude" / "src"))

from our_ark_provider_kit import (  # noqa: E402
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeConformanceMixin,
    RuntimeExecutionControl,
)
from our_ark_claude import ClaudeRuntime  # noqa: E402


class _Identity:
    name = "Test Agent"
    mission = "Exercise the Claude runtime provider."


class ClaudeRuntimeTests(unittest.TestCase):
    def test_streams_structured_result_usage_progress_and_events(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _write_fake_claude(root)
            capture = root / "args.jsonl"
            progress = []
            runtime = _runtime(root, executable)

            with patch.dict(os.environ, {"FAKE_CLAUDE_CAPTURE": str(capture)}, clear=False):
                result = runtime.respond(
                    _Identity(),
                    "hello",
                    cwd=root,
                    execution=RuntimeExecutionControl(
                        request_id="chat:1",
                        progress_callback=progress.append,
                    ),
                )

            args = _captured_args(capture)[0]

        self.assertEqual(result.final_text, "Claude completed the request.")
        self.assertEqual(result.session_id, "claude-session-1")
        self.assertEqual(result.usage.input_tokens, 17)
        self.assertEqual(result.usage.cached_input_tokens, 10)
        self.assertEqual(result.usage.output_tokens, 11)
        self.assertEqual([event.type for event in result.events], ["system", "assistant", "result"])
        self.assertEqual([item.stage for item in progress], ["started", "working", "completed"])
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(_argument(args, "--permission-mode"), "plan")
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_workspace_execution_uses_restricted_preapproved_tools(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _write_fake_claude(root)
            capture = root / "args.jsonl"
            runtime = _runtime(root, executable)

            with patch.dict(os.environ, {"FAKE_CLAUDE_CAPTURE": str(capture)}, clear=False):
                result = runtime.act_in_session(
                    _Identity(),
                    "edit README",
                    cwd=root,
                    session_key="task:1",
                )

            args = _captured_args(capture)[0]

        self.assertIn("--restricted", args)
        self.assertEqual(_argument(args, "--permission-mode"), "dontAsk")
        self.assertIn("Bash", _argument(args, "--tools"))
        self.assertIn("Edit", _argument(args, "--allowedTools"))
        self.assertEqual(len(result.side_effects), 1)
        self.assertEqual(result.side_effects[0].kind, "file")
        self.assertEqual(result.side_effects[0].reference, "README.md")

    def test_persistent_logical_session_resumes_native_claude_session(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _write_fake_claude(root)
            capture = root / "args.jsonl"
            runtime = _runtime(root, executable)

            with patch.dict(os.environ, {"FAKE_CLAUDE_CAPTURE": str(capture)}, clear=False):
                first = runtime.respond(_Identity(), "first", cwd=root, session_key="chat:room")
                second = runtime.respond(_Identity(), "second", cwd=root, session_key="chat:room")

            calls = _captured_args(capture)

        self.assertEqual(first.session_id, "claude-session-1")
        self.assertEqual(second.session_id, "claude-session-1")
        self.assertNotIn("--resume", calls[0])
        self.assertEqual(_argument(calls[1], "--resume"), "claude-session-1")

    def test_missing_stored_session_is_forgotten_and_recreated_once(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _write_fake_claude(root)
            capture = root / "args.jsonl"
            marker = root / "missing-session-seen"
            runtime = _runtime(root, executable)
            environment = {
                "FAKE_CLAUDE_CAPTURE": str(capture),
                "FAKE_CLAUDE_MODE": "missing-session-once",
                "FAKE_CLAUDE_MARKER": str(marker),
            }

            with patch.dict(os.environ, environment, clear=False):
                runtime.respond(_Identity(), "first", cwd=root, session_key="chat:room")
                result = runtime.respond(_Identity(), "second", cwd=root, session_key="chat:room")

            calls = _captured_args(capture)

        self.assertEqual(result.final_text, "Claude completed the request.")
        self.assertEqual(len(calls), 3)
        self.assertIn("--resume", calls[1])
        self.assertNotIn("--resume", calls[2])

    def test_authentication_failure_uses_pauseable_access_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = _runtime(root, _write_fake_claude(root))

            with patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "auth-error"}, clear=False):
                with self.assertRaises(AgentRuntimeAccessUnavailable) as raised:
                    runtime.respond(_Identity(), "hello", cwd=root)

        self.assertIn("claude auth login", str(raised.exception).lower())

    def test_human_cancellation_stops_running_cli(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = _runtime(root, _write_fake_claude(root))
            cancelled = threading.Event()
            timer = threading.Timer(0.1, cancelled.set)
            timer.start()
            try:
                with patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}, clear=False):
                    with self.assertRaises(AgentRuntimeCancelled):
                        runtime.act_in_session(
                            _Identity(),
                            "wait",
                            cwd=root,
                            execution=RuntimeExecutionControl(
                                cancellation_event=cancelled,
                            ),
                        )
            finally:
                timer.cancel()

    def test_health_requires_cli_authentication(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = _runtime(root, _write_fake_claude(root))

            healthy = runtime.health(root)
            with patch.dict(os.environ, {"FAKE_CLAUDE_AUTH": "logged-out"}, clear=False):
                logged_out = runtime.health(root)

        self.assertTrue(healthy.passed)
        self.assertFalse(logged_out.passed)
        self.assertIn("claude auth login", logged_out.summary)

    def test_model_effort_budget_and_executable_are_configurable(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _write_fake_claude(root)
            capture = root / "args.jsonl"
            settings: dict[str, str] = {
                "executable": str(executable),
                "model": "sonnet",
                "reasoning_effort": "xhigh",
            }
            runtime = _runtime(root, executable, settings=settings)

            configured = runtime.configure(("max-budget", "1.25"), root)
            with patch.dict(os.environ, {"FAKE_CLAUDE_CAPTURE": str(capture)}, clear=False):
                runtime.respond(_Identity(), "hello", cwd=root)
            args = _captured_args(capture)[0]
            summary = runtime.model_summary(root)

        self.assertIn("$1.25", configured)
        self.assertEqual(_argument(args, "--model"), "sonnet")
        self.assertEqual(_argument(args, "--effort"), "xhigh")
        self.assertEqual(_argument(args, "--max-budget-usd"), "1.25")
        self.assertIn("AI model: sonnet", summary)
        self.assertIn("Reasoning effort: xhigh", summary)


class ClaudeRuntimeConformanceTests(AgentRuntimeConformanceMixin, unittest.TestCase):
    def create_runtime(self, root: Path) -> ClaudeRuntime:
        return _runtime(root, _write_fake_claude(root))


def _runtime(
    root: Path,
    executable: Path,
    *,
    settings: dict[str, str] | None = None,
) -> ClaudeRuntime:
    values = settings if settings is not None else {"executable": str(executable)}

    def write_setting(key: str, value: str | None, _root: Path | None = None) -> None:
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value

    return ClaudeRuntime(
        root=root,
        read_settings=lambda _root=None: values,
        write_setting=write_setting,
        session_path=root / "sessions.json",
        env_prefix="TEST_AGENT",
        agent_name="Test Agent",
    )


def _write_fake_claude(root: Path) -> Path:
    executable = root / "claude"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
if args == ["auth", "status"]:
    logged_in = os.environ.get("FAKE_CLAUDE_AUTH") != "logged-out"
    print(json.dumps({"loggedIn": logged_in, "authMethod": "oauth" if logged_in else "none"}))
    raise SystemExit(0 if logged_in else 1)

capture = os.environ.get("FAKE_CLAUDE_CAPTURE")
if capture:
    with Path(capture).open("a", encoding="utf-8") as output:
        output.write(json.dumps(args) + "\\n")

mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
if mode == "sleep":
    time.sleep(10)
if mode == "auth-error":
    print("Not logged in. Run claude auth login.", file=sys.stderr)
    raise SystemExit(1)
if mode == "missing-session-once" and "--resume" in args:
    marker = Path(os.environ["FAKE_CLAUDE_MARKER"])
    if not marker.exists():
        marker.write_text("seen", encoding="utf-8")
        print(json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "No conversation found for session id",
        }))
        raise SystemExit(1)

sys.stdin.read()
session_id = args[args.index("--resume") + 1] if "--resume" in args else "claude-session-1"
print(json.dumps({
    "type": "system",
    "subtype": "init",
    "session_id": session_id,
    "model": "claude-test",
}))
print(json.dumps({
    "type": "assistant",
    "session_id": session_id,
    "message": {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "README.md"}},
            {"type": "text", "text": "Claude completed the request."},
        ],
    },
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": session_id,
    "result": "Claude completed the request.",
    "usage": {
        "input_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 10,
        "output_tokens": 11,
    },
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _captured_args(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _argument(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


if __name__ == "__main__":
    unittest.main()
