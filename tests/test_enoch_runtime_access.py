"""Tasks pause (never fail) on runtime usage limits and resume automatically."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app.task_workflow import _raise_if_runtime_usage_limit
from enoch.backlog import add_backlog_item
from enoch.identity import load_identity
from enoch.providers import (
    AgentRuntimeAccessUnavailable,
    AgentRuntimeError,
    ChatEvent,
)
from enoch.runtime_access import (
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    RuntimeAccessResumer,
    backoff_delay,
    is_usage_limit_text,
    parse_reset_time,
    quota_access_check,
    resume_state_path,
)
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import begin_next_task, task_queue_status


FIVE_HOUR = "You've hit your 5-hour limit · resets 3:20pm (America/Los_Angeles)"
SESSION = "You've hit your session limit · resets 12:20am (America/Los_Angeles)"
# The exact failure text recorded for tasks #42-#47.
INCIDENT = f"Claude Code did not answer successfully: {FIVE_HOUR}"
# 19:17 UTC on the incident day is 11:17 in Los Angeles.
INCIDENT_AT = datetime(2026, 11, 24, 19, 17, tzinfo=timezone.utc)
RESET_AT = datetime(2026, 11, 24, 23, 20, tzinfo=timezone.utc)  # 3:20pm PST
_ORIGINAL_MAYBE_START = EnochApplication._maybe_start_task_worker


class UsageLimitDetectionTests(unittest.TestCase):
    def test_incident_wording_is_a_usage_limit(self) -> None:
        for text in (
            FIVE_HOUR,
            INCIDENT,
            FIVE_HOUR.replace("'", "’"),
            SESSION,
            "Enoch could not complete queued task #42: " + INCIDENT,
            "You've hit your weekly limit · resets Nov 28, 9am",
            "Your 7-day limit resets Monday",
            "5h limit reached · resets 3pm",
            "Claude AI usage limit reached|1795994400",
            "You're out of extra usage · resets 3:20pm",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_usage_limit_text(text))

    def test_other_failures_are_not_usage_limits(self) -> None:
        for text in (
            "",
            "Tests failed: 3 errors.",
            "Prompt is too long: context limit exceeded",
            "Maximum number of turns reached",
            "connection reset by peer",
            "Worker execution failed",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_usage_limit_text(text))

    def test_workflow_safety_net_turns_limit_errors_into_access_errors(self) -> None:
        with self.assertRaises(AgentRuntimeAccessUnavailable) as raised:
            _raise_if_runtime_usage_limit(AgentRuntimeError(INCIDENT))
        self.assertIn("5-hour limit", str(raised.exception))
        _raise_if_runtime_usage_limit(AgentRuntimeError("Tests failed."))
        _raise_if_runtime_usage_limit(OSError(INCIDENT))  # Not a runtime error.


class ResetTimeParsingTests(unittest.TestCase):
    def test_clock_time_with_zone_is_the_next_occurrence(self) -> None:
        self.assertEqual(parse_reset_time(FIVE_HOUR, now=INCIDENT_AT), RESET_AT)
        self.assertEqual(
            parse_reset_time(SESSION, now=INCIDENT_AT),
            datetime(2026, 11, 25, 8, 20, tzinfo=timezone.utc),  # 12:20am PST
        )

    def test_reset_just_passed_is_not_pushed_a_day(self) -> None:
        now = RESET_AT + timedelta(minutes=2)
        self.assertEqual(parse_reset_time(FIVE_HOUR, now=now), RESET_AT)

    def test_dates_iso_and_epoch(self) -> None:
        self.assertEqual(
            parse_reset_time("weekly limit · resets Nov 28, 9am (UTC)", now=INCIDENT_AT),
            datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_reset_time("limit resets at 2026-11-24T23:20:00Z", now=INCIDENT_AT),
            RESET_AT,
        )
        self.assertEqual(
            parse_reset_time("Claude AI usage limit reached|1795994400", now=INCIDENT_AT),
            datetime.fromtimestamp(1795994400, timezone.utc),
        )

    def test_without_a_reset_time(self) -> None:
        for text in ("You've hit your limit", "resets 5", "Your 7-day limit resets Monday"):
            with self.subTest(text=text):
                self.assertIsNone(parse_reset_time(text, now=INCIDENT_AT))
        self.assertIsNone(
            parse_reset_time("resets at 2027-06-01T00:00:00Z", now=INCIDENT_AT),
            "implausibly distant reset times are ignored",
        )


class QuotaInterpretationTests(unittest.TestCase):
    def test_quota_snapshots(self) -> None:
        now = INCIDENT_AT
        reset = (RESET_AT).isoformat()
        clear = {"windows": [{"label": "5h", "used_percent": 13, "resets_at": reset}]}
        blocked = {"windows": [
            {"label": "5h", "used_percent": 100, "resets_at": reset},
            {"label": "7d", "used_percent": 36, "resets_at": None},
        ]}
        model_only = {"windows": [
            {"label": "5h", "used_percent": 20, "resets_at": reset},
            {"label": "7d / Opus", "used_percent": 100, "resets_at": reset},
        ]}
        expired = {"windows": [{"label": "5h", "used_percent": 100, "resets_at": "2026-11-24T19:00:00Z"}]}

        self.assertEqual(quota_access_check(clear, now=now).verdict, "available")
        check = quota_access_check(blocked, now=now)
        self.assertEqual((check.verdict, check.resets_at), ("unavailable", RESET_AT))
        self.assertEqual(quota_access_check(model_only, now=now).verdict, "unknown")
        self.assertEqual(quota_access_check(expired, now=now).verdict, "available")
        self.assertEqual(quota_access_check({"error": "Quota unavailable."}, now=now).verdict, "unknown")
        self.assertEqual(quota_access_check(None, now=now).verdict, "unknown")


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Harness:
    """Fake collaborators for RuntimeAccessResumer."""

    def __init__(self, root: Path, *, now: datetime = INCIDENT_AT) -> None:
        self.root = root
        self.clock = _Clock(now)
        self.paused = [1, 2]
        self.quota_result: object = None
        self.probe_error: Exception | None = None
        self.quota_calls: list[datetime] = []
        self.probe_calls: list[datetime] = []
        self.resumed: list[tuple] = []
        self.notified: list[tuple] = []

    def resumer(self) -> RuntimeAccessResumer:
        return RuntimeAccessResumer(
            self.root,
            paused_tasks=lambda: tuple(self.paused),
            quota=self._quota,
            probe=self._probe,
            resume=self._resume,
            notify=self.notified.append,
            clock=self.clock,
        )

    def _quota(self):
        self.quota_calls.append(self.clock.now)
        return self.quota_result

    def _probe(self) -> None:
        self.probe_calls.append(self.clock.now)
        if self.probe_error is not None:
            raise self.probe_error

    def _resume(self):
        resumed = tuple(self.paused)
        self.paused = []
        self.resumed.append(resumed)
        return resumed


class RuntimeAccessResumerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.harness = _Harness(Path(directory.name))
        self.resumer = self.harness.resumer()

    def advance(self, delta: timedelta) -> None:
        self.harness.clock.now += delta

    def test_waits_for_the_parsed_reset_then_resumes_once(self) -> None:
        next_check = self.resumer.record_pause(f"Claude runtime access is temporarily unavailable: {FIVE_HOUR}")
        self.assertEqual(next_check, RESET_AT + timedelta(minutes=1))
        self.harness.quota_result = {"windows": [{"label": "5h", "used_percent": 0, "resets_at": None}]}

        # Polling before the reset never touches quota or the runtime.
        for _ in range(30):
            self.advance(timedelta(minutes=8))
            if self.harness.clock.now >= next_check:
                break
            self.assertEqual(self.resumer.check_once(), ())
        self.assertEqual(self.harness.quota_calls, [])
        self.assertEqual(self.harness.probe_calls, [])

        self.harness.clock.now = next_check
        self.assertEqual(self.resumer.check_once(), (1, 2))
        self.assertEqual(self.harness.resumed, [(1, 2)])
        self.assertEqual(self.harness.notified, [(1, 2)])
        self.assertEqual(len(self.harness.quota_calls), 1)
        self.assertEqual(self.harness.probe_calls, [], "quota confirmed access; no probe needed")

        self.advance(timedelta(minutes=5))
        self.assertEqual(self.resumer.check_once(), ())
        self.assertEqual(len(self.harness.notified), 1)

    def test_structured_reset_time_wins(self) -> None:
        structured = datetime(2026, 11, 24, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(
            self.resumer.record_pause("Request blocked.", resets_at=structured),
            structured + timedelta(minutes=1),
        )

    def test_unknown_reset_backs_off_and_caps_checks(self) -> None:
        self.harness.quota_result = {"error": "get_usage is unsupported"}
        self.harness.probe_error = AgentRuntimeAccessUnavailable("Claude runtime access is temporarily unavailable.")
        first = self.resumer.record_pause("Claude runtime access is temporarily unavailable.")
        self.assertEqual(first, INCIDENT_AT + INITIAL_BACKOFF)

        # Poll every minute for 12 hours.
        for _ in range(12 * 60):
            self.advance(timedelta(minutes=1))
            self.resumer.check_once()

        probes = self.harness.probe_calls
        gaps = [later - earlier for earlier, later in zip(probes, probes[1:])]
        self.assertEqual(probes[0], first)
        self.assertEqual(
            gaps[:4],
            [timedelta(minutes=10), timedelta(minutes=20), timedelta(minutes=40), MAX_BACKOFF],
        )
        self.assertTrue(all(gap == MAX_BACKOFF for gap in gaps[3:]))
        self.assertLessEqual(len(probes), 16, "at most one check per hour once backed off")
        self.assertEqual(self.harness.resumed, [])
        self.assertEqual(self.harness.notified, [])
        self.assertEqual(backoff_delay(100), MAX_BACKOFF)

    def test_probe_reports_a_new_reset_time(self) -> None:
        self.harness.quota_result = None  # No quota reader on this runtime.
        self.harness.probe_error = AgentRuntimeAccessUnavailable(
            "Claude runtime access is temporarily unavailable: "
            "You've hit your weekly limit · resets Nov 26, 10am (UTC)"
        )
        self.resumer.record_pause("access unavailable")
        self.advance(INITIAL_BACKOFF)

        self.assertEqual(self.resumer.check_once(), ())

        self.assertEqual(
            self.resumer.next_check_at(),
            datetime(2026, 11, 26, 10, 1, tzinfo=timezone.utc),
        )

    def test_quota_blocked_window_defers_to_its_reset_without_probing(self) -> None:
        reset = INCIDENT_AT + timedelta(hours=3)
        self.harness.quota_result = {
            "windows": [{"label": "5h", "used_percent": 100, "resets_at": reset.isoformat()}]
        }
        self.resumer.record_pause("access unavailable")
        self.advance(INITIAL_BACKOFF)

        self.assertEqual(self.resumer.check_once(), ())

        self.assertEqual(self.harness.probe_calls, [])
        self.assertEqual(self.resumer.next_check_at(), reset + timedelta(minutes=1))

    def test_probe_verifies_when_quota_is_unavailable(self) -> None:
        self.harness.quota_result = {"error": "Quota unavailable."}
        self.resumer.record_pause(FIVE_HOUR)
        self.harness.clock.now = RESET_AT + timedelta(minutes=1)

        self.assertEqual(self.resumer.check_once(), (1, 2))
        self.assertEqual(len(self.harness.probe_calls), 1)

    def test_schedule_survives_a_restart(self) -> None:
        self.resumer.record_pause(FIVE_HOUR)
        restarted = self.harness.resumer()

        self.assertEqual(restarted.next_check_at(), RESET_AT + timedelta(minutes=1))
        state = json.loads(resume_state_path(self.harness.root).read_text())
        self.assertEqual(state["resets_at"], RESET_AT.isoformat())

    def test_pause_found_without_schedule_starts_with_back_off(self) -> None:
        # E.g. a task paused by an older daemon before this monitor existed.
        self.assertEqual(self.resumer.check_once(), ())
        self.assertEqual(self.resumer.next_check_at(), INCIDENT_AT + INITIAL_BACKOFF)
        self.assertEqual(self.harness.quota_calls, [])

    def test_pausing_again_right_after_resume_keeps_backing_off(self) -> None:
        self.harness.quota_result = {"windows": [{"label": "5h", "used_percent": 1, "resets_at": None}]}
        self.resumer.record_pause("access unavailable")
        self.advance(INITIAL_BACKOFF)
        self.assertEqual(self.resumer.check_once(), (1, 2))

        self.advance(timedelta(minutes=1))
        self.harness.paused = [1]
        self.assertEqual(
            self.resumer.record_pause("access unavailable"),
            self.harness.clock.now + 2 * INITIAL_BACKOFF,
        )

    def test_nothing_paused_does_nothing(self) -> None:
        self.harness.paused = []
        self.assertEqual(self.resumer.check_once(), ())
        self.assertEqual(self.harness.quota_calls, [])
        self.assertEqual(self.harness.probe_calls, [])


class _Chat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edits.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _QuotaRuntime:
    """Stands in for the Claude runtime's account quota reader."""

    name = "claude"

    def __init__(self, inner) -> None:
        self._inner = inner
        self.snapshot: dict = {"error": "Quota unavailable."}
        self.calls = 0

    def __getattr__(self, attribute):
        return getattr(self._inner, attribute)

    def quota(self, root=None):
        self.calls += 1
        return self.snapshot


class ApplicationPauseAndResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in (
            "enoch.app.core._sync_session_activity",
            "enoch.app.core.log_conversation_turn",
            "enoch.app.core.log_system_event",
            "enoch.app.core.ensure_long_term_memory",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        snapshot = patch(
            "enoch.app.core.EnochApplication._resolve_task_context_snapshot",
            return_value=TaskContextSnapshot(context="brief", source="chat-snapshot"),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        # Tests drive the worker by hand; never start a background thread.
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        self.start_worker = worker.start()
        self.addCleanup(worker.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _Chat()
        self.app = EnochApplication(load_identity(), self.root, self.chat)
        self.runtime = _QuotaRuntime(self.app.runtime)
        self.app.runtime = self.runtime
        self._update_id = 0

    def send(self, text: str) -> None:
        self._update_id += 1
        self.app.handle_event(
            ChatEvent(
                cursor=self._update_id,
                conversation_id=42,
                message_id=1000 + self._update_id,
                text=text,
            )
        )

    def texts(self) -> list[str]:
        return [text for _chat_id, text in self.chat.sent]

    def queue(self, *requests: str) -> None:
        for request in requests:
            self.send(f"/task {request}")

    def run_worker(self, error: Exception) -> list[int]:
        """Run the real queue worker loop with every runtime call failing."""

        started: list[int] = []

        def run_direct_work(*_args, **_kwargs):
            started.append(len(started) + 1)
            raise error

        self.app.runtime_access.clock = lambda: INCIDENT_AT
        with patch.object(self.app, "_run_direct_work", side_effect=run_direct_work):
            self.app._run_task_worker()
        return started

    def test_unrecognised_limit_error_pauses_instead_of_failing(self) -> None:
        self.queue("first")
        job = begin_next_task(self.root)
        assert job is not None
        self.app.runtime_access.clock = lambda: INCIDENT_AT

        with patch.object(self.app, "_run_direct_work", side_effect=AgentRuntimeError(INCIDENT)):
            self.app._run_task_job(job)

        status = task_queue_status(self.root)
        self.assertEqual([paused.id for paused in status.paused], [1])
        self.assertEqual(status.history, ())
        self.assertIn("5-hour limit", status.paused[0].result)
        self.assertIn("resumes automatically", status.paused[0].result)
        self.assertIn("/task resume 1", status.paused[0].result)
        self.assertNotIn("failed", self.texts()[-1].splitlines()[0])
        self.assertEqual(self.app.runtime_access.next_check_at(), RESET_AT + timedelta(minutes=1))

    def test_access_error_pauses_and_schedules_structured_reset(self) -> None:
        self.queue("first")
        job = begin_next_task(self.root)
        assert job is not None
        error = AgentRuntimeAccessUnavailable("Claude runtime access is temporarily unavailable: Request blocked.")
        error.resets_at = RESET_AT
        self.app.runtime_access.clock = lambda: INCIDENT_AT

        with patch.object(self.app, "_run_direct_work", side_effect=error):
            self.app._run_task_job(job)

        self.assertEqual(task_queue_status(self.root).paused[0].id, 1)
        self.assertEqual(self.app.runtime_access.next_check_at(), RESET_AT + timedelta(minutes=1))

    def test_queue_does_not_drain_after_a_pause(self) -> None:
        self.queue("first", "second", "third")
        add_backlog_item(42, "backlog idea", self.root)

        started = self.run_worker(AgentRuntimeError(INCIDENT))

        self.assertEqual(started, [1], "only the first task may hit the limit")
        status = task_queue_status(self.root)
        self.assertEqual([job.id for job in status.paused], [1])
        self.assertEqual([job.id for job in status.pending], [2, 3])
        self.assertEqual(status.history, ())
        self.assertIsNone(status.running)
        # Neither the worker nor backlog promotion starts more work.
        _ORIGINAL_MAYBE_START(self.app)
        self.assertIsNone(self.app._task_worker)
        self.assertIsNone(self.app._promote_next_backlog_if_idle())
        self.assertEqual(len(task_queue_status(self.root).pending), 2)

    def test_auto_resume_at_reset_in_original_order_with_one_line(self) -> None:
        self.queue("first", "second", "third")
        self.run_worker(AgentRuntimeError(INCIDENT))
        # A second task paused while access was unavailable.
        self.app.workflow.pause(2, result="paused for runtime access")
        sent_before = len(self.chat.sent)
        resumer = self.app.runtime_access

        before_reset = RESET_AT - timedelta(minutes=30)
        self.assertEqual(resumer.check_once(now=before_reset), ())
        self.assertEqual(self.runtime.calls, 0, "no checks before the reset time")

        self.runtime.snapshot = {
            "source": "Claude Code get_usage (experimental)",
            "windows": [
                {"label": "5h", "used_percent": 2, "resets_at": "2026-11-25T04:20:00+00:00"},
                {"label": "7d", "used_percent": 36, "resets_at": "2026-11-28T00:00:00+00:00"},
            ],
        }
        self.start_worker.reset_mock()
        resumed = resumer.check_once(now=RESET_AT + timedelta(minutes=1))

        self.assertEqual([job.id for job in resumed], [1, 2])
        status = task_queue_status(self.root)
        self.assertEqual(status.paused, ())
        self.assertEqual([job.id for job in status.pending], [1, 2, 3])
        self.assertEqual(
            self.texts()[sent_before:],
            ["▶️ Claude limit reset; resumed tasks #1, #2."],
        )
        self.start_worker.assert_called()
        self.assertEqual(load_task_events(self.root, task_id=1)[-1].trigger, "runtime-access-restored")
        self.assertEqual(load_task_events(self.root, task_id=1)[-1].event_actor, "system")

    def test_auto_resume_probes_when_quota_is_unavailable(self) -> None:
        self.queue("first")
        self.run_worker(AgentRuntimeError(INCIDENT))
        due = RESET_AT + timedelta(minutes=1)

        with patch.object(
            self.app,
            "_probe_runtime_access",
            side_effect=AgentRuntimeAccessUnavailable("Claude runtime access is temporarily unavailable."),
        ) as probe:
            self.assertEqual(self.app.runtime_access.check_once(now=due), ())
        probe.assert_called_once_with()
        self.assertEqual(task_queue_status(self.root).paused[0].id, 1)
        self.assertEqual(self.app.runtime_access.next_check_at(), due + 2 * timedelta(minutes=5))

        with patch.object(self.app, "_probe_runtime_access", return_value=None):
            resumed = self.app.runtime_access.check_once(now=due + timedelta(minutes=10))
        self.assertEqual([job.id for job in resumed], [1])
        self.assertEqual(self.texts()[-1], "▶️ Claude limit reset; resumed task #1.")

    def test_manual_resume_still_works_and_ends_the_auto_resume_cycle(self) -> None:
        self.queue("first", "second")
        self.run_worker(AgentRuntimeError(INCIDENT))

        self.send("/task resume 1")

        self.assertEqual(self.texts()[-1], "Resumed 1 task: #1.")
        self.assertEqual(load_task_events(self.root, task_id=1)[-1].trigger, "/task resume")
        self.assertEqual([job.id for job in task_queue_status(self.root).pending], [1, 2])
        sent = len(self.chat.sent)
        self.assertEqual(self.app.runtime_access.check_once(now=RESET_AT + timedelta(hours=1)), ())
        self.assertEqual(len(self.chat.sent), sent)
        self.assertEqual(self.runtime.calls, 0)
        self.assertIsNone(self.app.runtime_access.next_check_at())


if __name__ == "__main__":
    unittest.main()
