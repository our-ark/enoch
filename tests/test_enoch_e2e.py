from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.evolution.core import get_evolve_candidate
from enoch.identity import load_identity
from enoch.immune import DoctorDiagnosis, ImmuneResult
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import begin_next_task, task_queue_status
from enoch.app.core import EnochApplication
from our_ark_telegram import TelegramConfig, telegram_event


CHAT_ID = 42
RESIDENT_BRANCH = "agent/enoch-gary"
PR_URL = "https://github.com/our-ark/enoch/pull/900"


class EnochEvolutionEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.base = Path(self._temporary_directory.name)
        self.source = self.base / "source"
        self.instance = self.base / "instance"
        self.remote = self.base / "origin.git"
        self.bin_dir = self.base / "bin"
        self.codex_mode = self.base / "codex-mode"
        self.codex_log = self.base / "codex.jsonl"
        self.gh_log = self.base / "gh.jsonl"
        self._update_id = 0

        self._create_git_worktrees()
        self._create_fake_codex()
        self._create_fake_gh()
        self.codex_mode.write_text("success\n", encoding="utf-8")

        environment = patch.dict(
            os.environ,
            {
                "ENOCH_CODEX_BIN": str(self.bin_dir / "codex"),
                "FAKE_CODEX_MODE_FILE": str(self.codex_mode),
                "FAKE_CODEX_LOG": str(self.codex_log),
                "FAKE_GH_LOG": str(self.gh_log),
                "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

        bot_doctor = patch(
            "enoch.app.core.run_immune_system",
            side_effect=lambda _root=None, **_kwargs: _passing_doctor(),
        )
        publish_doctor = patch(
            "our_ark_github.workflow.run_immune_system",
            side_effect=lambda _root=None: _passing_doctor(),
        )
        bot_doctor.start()
        publish_doctor.start()
        self.addCleanup(bot_doctor.stop)
        self.addCleanup(publish_doctor.stop)

        self.client = _RecordingTelegramClient(CHAT_ID)
        self.bot = EnochApplication(load_identity(), self.instance, self.client)

    def test_evolve_approve_publishes_ready_pr_from_latest_main_with_one_progress_message(
        self,
    ) -> None:
        candidate_id = self._add_backlog_candidate("Improve evolve reliability")

        reply = self._command(f"/evolve approve {candidate_id}")
        self.assertIn("queued task #1", reply)
        self.client.clear()

        completed = self._run_next_task()

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.pr_urls, (PR_URL,))
        call = self._latest_gh_call()
        self.assertNotIn("--draft", call)
        body = _argument_value(call, "--body")
        self.assertIn(f"- Candidate: `{candidate_id}`", body)
        self.assertIn("- Evidence source: backlog", body)
        self.assertIn("- Signal actor: human", body)
        self.assertIn("- Candidate actor: agent", body)
        self.assertIn("- Approval actor: human", body)
        self.assertIn("- Task: #1", body)

        branch = _argument_value(call, "--head")
        self.assertEqual(completed.branch_name, branch)
        self.assertTrue(completed.worktree_path)
        self.assertFalse(Path(completed.worktree_path).exists())
        remote_head = _git(self.instance, "rev-parse", f"origin/{branch}").stdout.strip()
        remote_parent = _git(self.instance, "rev-parse", f"{remote_head}^").stdout.strip()
        self.assertEqual(remote_parent, self.latest_main_head)
        self.assertEqual(_current_branch(self.source), "main")
        self.assertEqual(_current_branch(self.instance), RESIDENT_BRANCH)
        self.assertFalse(_local_branch_exists(self.instance, branch))

        self.assertEqual(len(self.client.sent), 2)
        progress_message_id = self.client.sent[0][2]
        self.assertGreater(len(self.client.edited), 5)
        self.assertTrue(
            all(message_id == progress_message_id for _, message_id, _ in self.client.edited)
        )
        self.assertIn("Status: completed", self.client.edited[-1][2])
        self.assertIn(PR_URL, self.client.edited[-1][2])
        self.assertIn("Final status: completed", self.client.sent[-1][1])

        prompt = self._latest_codex_prompt()
        self.assertIn("Required pull request metadata:", prompt)
        self.assertIn("## Evolution provenance", prompt)
        self.assertIn(f"- Candidate: `{candidate_id}`", prompt)

    def test_evolve_retry_keeps_failed_history_and_links_new_task(self) -> None:
        candidate_id = self._add_backlog_candidate("Add a retry guardrail")
        self._command(f"/evolve approve {candidate_id}")
        self.client.clear()
        self._set_codex_mode("error")

        failed = self._run_next_task()

        self.assertEqual(failed.status, "failed")
        self.assertTrue(Path(failed.worktree_path).is_dir())
        self.assertEqual(get_evolve_candidate(candidate_id, self.instance).status, "failed")

        self._set_codex_mode("success")
        reply = self._command(f"/evolve retry {candidate_id}")
        self.assertIn("linked to failed task #1", reply)
        self.client.clear()

        completed = self._run_next_task()

        history = task_queue_status(self.instance).history
        self.assertEqual([(job.id, job.status) for job in history], [(1, "failed"), (2, "completed")])
        self.assertEqual(completed.parent_task_id, 1)
        self.assertNotEqual(completed.worktree_path, failed.worktree_path)
        self.assertTrue(Path(failed.worktree_path).is_dir())
        self.assertFalse(Path(completed.worktree_path).exists())
        self.assertEqual(get_evolve_candidate(candidate_id, self.instance).status, "done")
        body = _argument_value(self._latest_gh_call(), "--body")
        self.assertIn("- Task: #2", body)
        self.assertIn("- Retry of task: #1", body)

    def test_codex_auth_failure_pauses_and_resume_completes_same_task(self) -> None:
        candidate_id = self._add_backlog_candidate("Handle unavailable Codex access")
        self._command(f"/evolve approve {candidate_id}")
        self.client.clear()
        self._set_codex_mode("auth-fail")

        running = begin_next_task(self.instance)
        self.assertIsNotNone(running)
        self.bot._run_task_job(running)

        paused = task_queue_status(self.instance)
        self.assertEqual(paused.paused_count, 1)
        self.assertEqual(paused.paused[0].id, 1)
        self.assertIn("Task #1 paused", self.client.sent[-1][1])
        self.assertIn("use /task resume 1", self.client.sent[-1][1].lower())

        self._set_codex_mode("success")
        with patch.object(self.bot, "_maybe_start_task_worker"):
            reply = self._command("/task resume 1")
        self.assertEqual(reply, "Resumed 1 task: #1.")

        completed = self._run_next_task()

        self.assertEqual(completed.id, 1)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(get_evolve_candidate(candidate_id, self.instance).status, "done")
        events = [event.event for event in load_task_events(self.instance, task_id=1)]
        self.assertIn("paused", events)
        self.assertIn("resumed", events)
        self.assertEqual(events[-1], "completed")

    def test_failed_evolve_task_creates_experience_candidate_with_causal_provenance(
        self,
    ) -> None:
        parent_id = self._add_backlog_candidate("Prevent a recurring worker failure")
        self._command(f"/evolve approve {parent_id}")
        self.client.clear()
        self._set_codex_mode("error")
        failed = self._run_next_task()
        self.assertEqual(failed.status, "failed")

        experience = get_evolve_candidate("task-1", self.instance)
        self.assertEqual(experience.evidence_source, "experience")
        self.assertEqual(experience.signal_actor, "system")
        self.assertEqual(experience.candidate_actor, "agent")
        self.assertEqual(experience.parent_candidate_id, parent_id)
        self.assertEqual(experience.source_task_id, 1)

        self._set_codex_mode("success")
        reply = self._command("/evolve approve task-1")
        self.assertIn("queued task #2", reply)
        self.client.clear()
        completed = self._run_next_task()
        self.assertEqual(completed.status, "completed")

        body = _argument_value(self._latest_gh_call(), "--body")
        self.assertIn("- Candidate: `task-1`", body)
        self.assertIn("- Evidence source: experience", body)
        self.assertIn("- Signal actor: system", body)
        self.assertIn("- Candidate actor: agent", body)
        self.assertIn("- Approval actor: human", body)
        self.assertIn(f"- Parent candidate: `{parent_id}`", body)
        self.assertIn("- Source task: #1", body)
        self.assertIn("- Task: #2", body)

    def _create_git_worktrees(self) -> None:
        self.source.mkdir()
        _git(self.base, "init", "--bare", str(self.remote))
        _git(self.source, "init", "-b", "main")
        _git(self.source, "config", "user.name", "Enoch E2E")
        _git(self.source, "config", "user.email", "enoch-e2e@example.com")
        (self.source / ".gitignore").write_text(
            ".agent/instance.yaml\n.enoch/\n",
            encoding="utf-8",
        )
        (self.source / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "initial")
        _git(self.source, "remote", "add", "origin", str(self.remote))
        _git(self.source, "push", "-u", "origin", "main")
        _git(
            self.source,
            "worktree",
            "add",
            "-b",
            RESIDENT_BRANCH,
            str(self.instance),
            "main",
        )

        (self.source / "README.md").write_text("latest main\n", encoding="utf-8")
        _git(self.source, "add", "README.md")
        _git(self.source, "commit", "-m", "advance main")
        _git(self.source, "push", "origin", "main")
        self.latest_main_head = _git(self.source, "rev-parse", "main").stdout.strip()

        metadata = self.instance / ".agent" / "instance.yaml"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(
            "\n".join(
                [
                    "schema_version: 1",
                    "worktree:",
                    f'  path: "{self.instance}"',
                    f'  source_repo: "{self.source}"',
                    f'  branch: "{RESIDENT_BRANCH}"',
                    "  kind: agent-instance",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _create_fake_codex(self) -> None:
        self.bin_dir.mkdir()
        executable = self.bin_dir / "codex"
        executable.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path
                import sys

                args = sys.argv[1:]
                prompt = sys.stdin.read()
                mode = Path(os.environ["FAKE_CODEX_MODE_FILE"]).read_text(encoding="utf-8").strip()
                with Path(os.environ["FAKE_CODEX_LOG"]).open("a", encoding="utf-8") as log:
                    log.write(json.dumps({"args": args, "prompt": prompt}) + "\\n")
                if mode == "auth-fail":
                    print("401 Unauthorized: access token has expired", file=sys.stderr)
                    raise SystemExit(1)
                if mode == "error":
                    print("worker execution failed", file=sys.stderr)
                    raise SystemExit(1)

                cwd = Path(args[args.index("--cd") + 1]) if "--cd" in args else Path.cwd()
                (cwd / "EVOLVE_RESULT.md").write_text(
                    "Implemented by the hermetic Codex worker.\\n",
                    encoding="utf-8",
                )
                output = Path(args[args.index("--output-last-message") + 1])
                output.write_text("Implemented the requested evolve change.", encoding="utf-8")
                print(json.dumps({"type": "thread.started", "thread_id": "e2e-session"}))
                print(json.dumps({
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 1
                    }
                }))
                """
            ).lstrip(),
            encoding="utf-8",
        )
        executable.chmod(0o755)

    def _create_fake_gh(self) -> None:
        executable = self.bin_dir / "gh"
        executable.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path
                import sys

                with Path(os.environ["FAKE_GH_LOG"]).open("a", encoding="utf-8") as log:
                    log.write(json.dumps(sys.argv[1:]) + "\\n")
                print({PR_URL!r})
                """
            ).lstrip(),
            encoding="utf-8",
        )
        executable.chmod(0o755)

    def _add_backlog_candidate(self, text: str) -> str:
        item = add_backlog_item(CHAT_ID, text, self.instance, priority="p0")
        candidate_id = f"backlog-{item.id}"
        self.assertEqual(get_evolve_candidate(candidate_id, self.instance).status, "candidate")
        return candidate_id

    def _command(self, text: str) -> str:
        self._update_id += 1
        event = telegram_event(_message_update(self._update_id, text))
        self.assertIsNotNone(event)
        self.bot.handle_event(event)
        return self.client.sent[-1][1]

    def _run_next_task(self):
        job = begin_next_task(self.instance)
        self.assertIsNotNone(job)
        self.bot._run_task_job(job)
        completed = next(
            item for item in reversed(task_queue_status(self.instance).history) if item.id == job.id
        )
        return completed

    def _set_codex_mode(self, mode: str) -> None:
        self.codex_mode.write_text(f"{mode}\n", encoding="utf-8")

    def _latest_gh_call(self) -> list[str]:
        calls = [
            json.loads(line)
            for line in self.gh_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(calls)
        return calls[-1]

    def _latest_codex_prompt(self) -> str:
        calls = [
            json.loads(line)
            for line in self.codex_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(calls)
        return calls[-1]["prompt"]


class _RecordingTelegramClient:
    def __init__(self, allowed_chat_id: int) -> None:
        self.config = TelegramConfig(token="test-token", allowed_chat_id=allowed_chat_id)
        self.sent: list[tuple[int, str, int]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.acks: list[tuple[int, int]] = []
        self._next_message_id = 2000

    def send_message(self, chat_id: int, text: str) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, text, self._next_message_id))
        return self._next_message_id

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self.edited.append((chat_id, message_id, text))

    def send_read_ack(self, chat_id: int, message_id: int) -> None:
        self.acks.append((chat_id, message_id))

    def clear(self) -> None:
        self.sent.clear()
        self.edited.clear()
        self.acks.clear()


def _passing_doctor() -> ImmuneResult:
    return ImmuneResult(
        passed=True,
        command="hermetic-e2e-doctor",
        output="OK",
        diagnosis=DoctorDiagnosis(
            summary="Hermetic E2E health checks passed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="No repair needed.",
        ),
        checks=[],
    )


def _message_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1000 + update_id,
            "chat": {"id": CHAT_ID},
            "text": text,
        },
    }


def _argument_value(arguments: list[str], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


def _current_branch(root: Path) -> str:
    return _git(root, "branch", "--show-current").stdout.strip()


def _local_branch_exists(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )


if __name__ == "__main__":
    unittest.main()
