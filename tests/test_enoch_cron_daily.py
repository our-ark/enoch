"""Daily cron acceptance, durable retries, and bound-chat integration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from enoch.app.core import EnochApplication, TaskContextSnapshot, _with_replied_text_context
from enoch.app.reporting import _format_cron_details, _format_tasks_report, _task_status_message
from enoch.cron import (
    add_cron_job, cancel_cron_job, claim_due_cron_jobs, cron_path,
    cron_scheduler_wait_seconds, cron_status, find_cron_job, pause_cron_job,
    record_cron_task, request_cron_job_run, resume_cron_job,
)
from enoch.identity import load_identity
from our_ark_slack.core import _translate_secondary_command
from enoch.private_state import migrate_private_state
from enoch.state import StateCorruptionError, file_transaction
from enoch.tasks.queue import begin_next_task, complete_task, task_queue_status
from tests.test_enoch_telegram import FakeTelegramClient, _handle_update, _message_update


ZONE = "America/Los_Angeles"
TODAY = datetime(2026, 9, 15, 19, tzinfo=timezone.utc)


def daily(root, *, chat_id=42, now=TODAY, key=""):
    return add_cron_job(
        chat_id, "daily test work", root=root, cadence="daily", daily_time="18:00",
        timezone=ZONE, now=now, idempotency_key=key,
    )


class DailyCronTests(unittest.TestCase):
    def test_frozen_today_and_both_dst_transitions_keep_local_1800(self):
        cases = [
            (TODAY, "2026-09-15T18:00:00-07:00", "2026-09-16T01:00:00+00:00"),
            (datetime(2026, 3, 7, 20, tzinfo=timezone.utc), "2026-03-07T18:00:00-08:00", "2026-03-08T02:00:00+00:00"),
            (datetime(2026, 10, 31, 20, tzinfo=timezone.utc), "2026-10-31T18:00:00-07:00", "2026-11-01T01:00:00+00:00"),
        ]
        for current, local, utc in cases:
            with self.subTest(current=current), TemporaryDirectory() as temp:
                root = Path(temp)
                job = daily(root, now=current)
                self.assertEqual(job.next_run_at, utc)
                self.assertEqual(datetime.fromisoformat(utc).astimezone(ZoneInfo(ZONE)).isoformat(), local)
                report = _format_cron_details(job)
                self.assertIn(local, report)
                self.assertIn(utc + " UTC", report)
                due, = claim_due_cron_jobs(root, now=datetime.fromisoformat(utc))
                after = record_cron_task(job.id, 1, root, claim_id=due.claim_id, now=datetime.fromisoformat(utc))
                next_local = datetime.fromisoformat(after.next_run_at).astimezone(ZoneInfo(ZONE))
                self.assertEqual((next_local.hour, next_local.minute), (18, 0))
                expected = {
                    3: "2026-03-09T01:00:00+00:00",
                    10: "2026-11-02T02:00:00+00:00",
                    9: "2026-09-17T01:00:00+00:00",
                }
                self.assertEqual(after.next_run_at, expected[current.month])

    def test_invalid_declarations_and_missing_binding_create_no_state(self):
        for overrides in (
            *({"daily_time": value} for value in ("", "6:00", "24:00", "18:60", "18:00:00", "noon")),
            {"timezone": "Unknown/Nowhere"}, {"timezone": ""}, {"chat_id": None},
        ):
            with self.subTest(overrides=overrides), TemporaryDirectory() as temp:
                root = Path(temp)
                options = dict(chat_id=42, text="work", root=root, cadence="daily", daily_time="18:00", timezone=ZONE)
                options.update(overrides)
                with self.assertRaises(ValueError):
                    add_cron_job(**options)
                self.assertFalse(cron_path(root).exists())

    def test_schema_four_migration_preserves_all_jobs_context_binding_and_claim(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            old = add_cron_job(42, "interval work", 600, root, now=TODAY, context="saved", context_source="chat", idempotency_key="old")
            cancelled = cancel_cron_job(add_cron_job("room-b", "past work", 60, root, now=TODAY).id, root)
            due, = claim_due_cron_jobs(root, now=datetime(2026, 9, 15, 20, tzinfo=timezone.utc))
            new_fields = {"cadence", "daily_time", "timezone", "claim_kind", "claim_scheduled_for", "run_now_id", "run_now_key", "run_now_history", "paused_at"}
            def legacy(job):
                return {key: value for key, value in asdict(job).items() if key not in new_fields}
            payload = dict(schema_version=4, next_id=3, active=[legacy(due)], history=[legacy(cancelled)])
            cron_path(root).write_text(json.dumps(payload))
            loaded, = cron_status(root).active
            self.assertEqual(loaded.cadence, "interval")
            self.assertEqual(loaded.claim_id, due.claim_id)
            self.assertEqual(loaded.claim_scheduled_for, old.next_run_at)
            self.assertEqual(claim_due_cron_jobs(root, now=TODAY), (loaded,))
            self.assertTrue(migrate_private_state(root).applied)
            migrated = json.loads(cron_path(root).read_text())
            self.assertEqual(migrated["schema_version"], 5)
            for key in ("active", "history"):
                self.assertEqual(len(migrated[key]), len(payload[key]))
                for before, after in zip(payload[key], migrated[key]):
                    self.assertTrue(before.items() <= after.items())
            new = daily(root)
            self.assertEqual(new.id, 3)
            self.assertEqual(len(cron_status(root).active), 2)
            self.assertEqual(cron_status(root).history[0].chat_id, "room-b")

    def test_corruption_is_reported_and_never_overwritten(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            daily(root)
            valid = json.loads(cron_path(root).read_text())
            variants = ["{broken", "[]"]
            for changes in ({"daily_time": "24:00"}, {"timezone": "Bogus/Zone"}, {"chat_id": None}, {"cadence": "weekly"}, {"next_run_at": "bad"}, {"run_now_history": [""]}):
                payload = json.loads(json.dumps(valid))
                payload["active"][0].update(changes)
                variants.append(json.dumps(payload))
            for raw in variants:
                with self.subTest(raw=raw):
                    cron_path(root).write_text(raw)
                    with self.assertRaises(StateCorruptionError):
                        cron_status(root)
                    with self.assertRaises(StateCorruptionError):
                        daily(root)
                    self.assertEqual(cron_path(root).read_text(), raw)

    def test_restart_coalesces_missed_days_and_retries_same_claim_and_ack(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = daily(root)
            restarted = datetime(2026, 9, 20, 22, tzinfo=timezone.utc)
            due, = claim_due_cron_jobs(root, now=restarted)
            self.assertEqual(due.next_run_at, job.next_run_at)
            self.assertEqual(claim_due_cron_jobs(root, now=restarted), (due,))
            ack = record_cron_task(job.id, 7, root, claim_id=due.claim_id, now=restarted)
            self.assertEqual(ack.next_run_at, "2026-09-21T01:00:00+00:00")
            self.assertEqual(ack.last_scheduled_at, job.next_run_at)
            self.assertIsNone(record_cron_task(job.id, 7, root, claim_id=due.claim_id, now=restarted))
            self.assertEqual(claim_due_cron_jobs(root, now=restarted), ())

    def test_lifecycle_persists_pause_retains_claim_and_cancels_pending_run_now(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = daily(root)
            request_cron_job_run(job.id, root, idempotency_key="r1")
            self.assertEqual(cron_scheduler_wait_seconds(root, now=TODAY), 0)
            paused = pause_cron_job(job.id, root, now=TODAY)
            self.assertFalse(paused.run_now_id)
            self.assertEqual(pause_cron_job(job.id, root), paused)
            self.assertEqual(cron_status(root).paused_count, 1)
            self.assertEqual(cron_status(root).active_count, 0)
            self.assertEqual(claim_due_cron_jobs(root), ())
            self.assertEqual(cron_scheduler_wait_seconds(root), 5)
            with self.assertRaisesRegex(ValueError, "paused"):
                request_cron_job_run(job.id, root)
            resumed = resume_cron_job(job.id, root)
            self.assertEqual(resume_cron_job(job.id, root), resumed)
            self.assertFalse(request_cron_job_run(job.id, root, idempotency_key="r1").run_now_id)
            due, = claim_due_cron_jobs(root, now=datetime(2026, 9, 18, tzinfo=timezone.utc))
            pause_cron_job(job.id, root)
            self.assertEqual(claim_due_cron_jobs(root), ())
            self.assertEqual(find_cron_job(job.id, root).claim_id, due.claim_id)
            resume_cron_job(job.id, root)
            self.assertEqual(claim_due_cron_jobs(root)[0].claim_id, due.claim_id)
            cancel_cron_job(job.id, root)
            self.assertIsNone(resume_cron_job(job.id, root))
            self.assertIsNone(request_cron_job_run(job.id, root))
            self.assertEqual(find_cron_job(job.id, root).status, "cancelled")

    def test_run_now_receipts_coalesce_and_do_not_shift_daily_or_interval(self):
        for cadence in ("daily", "interval"):
            with self.subTest(cadence=cadence), TemporaryDirectory() as temp:
                root = Path(temp)
                job = daily(root) if cadence == "daily" else add_cron_job(42, "work", 3600, root, now=TODAY)
                requested = request_cron_job_run(job.id, root, idempotency_key="r1")
                self.assertEqual(request_cron_job_run(job.id, root, idempotency_key="r1"), requested)
                request_cron_job_run(job.id, root, idempotency_key="r2")
                due, = claim_due_cron_jobs(root, now=TODAY)
                self.assertEqual(due.claim_kind, "run-now")
                self.assertEqual(claim_due_cron_jobs(root, now=TODAY), (due,))
                request_cron_job_run(job.id, root, idempotency_key="r3")
                ack = record_cron_task(job.id, 1, root, claim_id=due.claim_id, now=TODAY)
                self.assertEqual(ack.next_run_at, job.next_run_at)
                for key in ("r1", "r2", "r3"):
                    self.assertFalse(request_cron_job_run(job.id, root, idempotency_key=key).run_now_id)
                self.assertEqual(claim_due_cron_jobs(root, now=TODAY), ())
                request_cron_job_run(job.id, root, idempotency_key="r4")
                due, = claim_due_cron_jobs(root, now=datetime.fromisoformat(job.next_run_at))
                self.assertEqual(due.claim_kind, "scheduled")
                record_cron_task(job.id, 2, root, claim_id=due.claim_id, now=datetime.fromisoformat(job.next_run_at))
                self.assertEqual(claim_due_cron_jobs(root, now=datetime.fromisoformat(job.next_run_at)), ())

    def test_instances_chats_and_creation_receipts_are_isolated(self):
        with TemporaryDirectory() as temp:
            first, second = Path(temp) / "one", Path(temp) / "two"
            a = daily(first, key="same")
            b = daily(first, chat_id="room-b", key="same")
            c = daily(second, key="same")
            self.assertEqual(daily(first, key="same"), a)
            self.assertNotEqual(a.id, b.id)
            self.assertEqual(a.id, c.id)
            for operation in (find_cron_job, pause_cron_job, resume_cron_job, request_cron_job_run, cancel_cron_job):
                self.assertIsNone(operation(a.id, first, chat_id="room-b"))
            self.assertEqual(cron_status(first, chat_id="room-b").active, (b,))
            pause_cron_job(a.id, first)
            self.assertEqual(cron_status(second).active, (c,))


class DailyCronApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeTelegramClient(allowed_chat_id=42)
        self.client.command_prefix = "."
        self.app = EnochApplication(load_identity(), self.root, self.client)
        for target in ("log_conversation_turn", "ensure_long_term_memory", "_sync_session_activity"):
            patcher = patch("enoch.app.core." + target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.app, "_resolve_task_context_snapshot", return_value=TaskContextSnapshot(context="Saved context.", source="chat-snapshot"))
        self.snapshot = patcher.start()
        self.addCleanup(patcher.stop)

    def test_dot_commands_help_show_controls_and_chat_scope(self):
        with patch("enoch.cron._utc_now", return_value=TODAY):
            for event_id, text in enumerate((
                ".cron daily 18:00 America/Los_Angeles daily work", ".help cron",
                ".cron show 1", ".cron pause 1", ".cron", ".cron resume 1",
                ".cron run-now 1", ".cron cancel 1",
            ), 1):
                _handle_update(self.app, _message_update(update_id=event_id, chat_id=42, text=_translate_secondary_command(text)))
        replies = [text for _, text in self.client.sent]
        self.assertIn("2026-09-15T18:00:00-07:00", replies[0])
        self.assertIn("2026-09-16T01:00:00+00:00 UTC", replies[0])
        for command in ("daily", "pause", "resume", "run-now", "show"):
            self.assertIn(command, replies[1])
        self.assertIn("Saved context.", cron_status(self.root).history[0].context)
        self.assertIn("Bound chat: 42", replies[2])
        self.assertIn("Paused:", replies[4])
        self.assertIn("#1 [paused]", replies[4])
        self.assertIn("Cancelled cron #1", replies[7])
        for command in ("show", "pause", "resume", "run-now", "cancel"):
            text = f"/cron {command} 1"
            self.assertEqual(_with_replied_text_context(text, "unrelated reply", provider_name="telegram"), text)
        other = daily(self.root, chat_id=99)
        self.assertNotIn(f"#{other.id} ", self.app._cron(42, "/cron"))
        for command in ("show", "pause", "resume", "run-now", "cancel"):
            reply = self.app._cron(42, f"/cron {command} {other.id}")
            self.assertNotIn("daily test work", reply)
        self.assertEqual(find_cron_job(other.id, self.root), other)

    def test_invalid_command_is_rejected_before_context_resolution(self):
        for text in ("/cron daily 24:00 UTC work", "/cron daily 18:00 Bad/Zone work", "/cron daily 18:00 UTC"):
            self.app._cron(42, text)
        self.snapshot.assert_not_called()
        self.assertEqual(cron_status(self.root).active, ())
        for chat_id in (None, "", 0, False):
            self.assertIn("bound chat", self.app._cron(chat_id, "/cron daily 18:00 UTC work"))
        self.snapshot.assert_not_called()

    def test_pause_counts_in_both_task_summaries(self):
        job = daily(self.root)
        pause_cron_job(job.id, self.root)
        for report in (_format_tasks_report(self.root), _task_status_message(self.root)):
            self.assertIn("0 active, 1 paused", report)

    def test_pause_after_claim_prevents_admission_and_resume_preserves_occurrence(self):
        for cadence in ("daily", "interval"):
            for kind in ("scheduled", "run-now"):
                with self.subTest(cadence=cadence, kind=kind), TemporaryDirectory() as temp:
                    root = Path(temp)
                    app = EnochApplication(load_identity(), root, self.client)
                    job = daily(root) if cadence == "daily" else add_cron_job(
                        42, "interval work", 3600, root, now=TODAY,
                    )
                    current = datetime.fromisoformat(job.next_run_at) if kind == "scheduled" else TODAY
                    if kind == "run-now":
                        request_cron_job_run(job.id, root, idempotency_key="manual")
                    claimed = []

                    def claim_then_pause(root):
                        occurrences = claim_due_cron_jobs(root)
                        claimed.extend(occurrences)
                        self.assertIn("Paused cron #1.", app._cron(42, "/cron pause 1"))
                        self.assertEqual(task_queue_status(root).pending_count, 0)
                        return occurrences

                    with patch("enoch.cron._utc_now", return_value=current):
                        with patch("enoch.app.core.claim_due_cron_jobs", side_effect=claim_then_pause):
                            self.assertEqual(app._enqueue_due_cron_jobs(), ())
                        paused = find_cron_job(job.id, root)
                        self.assertEqual(task_queue_status(root).pending_count, 0)
                        self.assertEqual(paused.status, "paused")
                        self.assertEqual(paused.claim_id, claimed[0].claim_id)
                        self.assertEqual(paused.claim_kind, kind)
                        self.assertEqual(paused.claim_scheduled_for, claimed[0].claim_scheduled_for)
                        self.assertEqual(paused.next_run_at, job.next_run_at)
                        self.assertIsNone(paused.last_task_id)
                        self.assertEqual(app._enqueue_due_cron_jobs(), ())
                        self.assertIn("Resumed cron #1.", app._cron(42, "/cron resume 1"))
                        admitted, = app._enqueue_due_cron_jobs()
                        self.assertEqual(admitted.idempotency_key, f"cron:{job.id}:{claimed[0].claim_id}")
                        self.assertEqual(task_queue_status(root).pending_count, 1)
                        self.assertEqual(app._enqueue_due_cron_jobs(), ())
                        resumed = find_cron_job(job.id, root)
                        self.assertFalse(resumed.claim_id)
                        self.assertEqual(resumed.last_task_id, admitted.id)
                        if kind == "run-now":
                            self.assertEqual(resumed.next_run_at, job.next_run_at)
                        else:
                            self.assertGreater(resumed.next_run_at, job.next_run_at)

    def test_pause_waits_for_admission_that_already_holds_the_cron_transaction(self):
        job = daily(self.root)
        pause_attempted = threading.Event()
        pause_thread_id = None
        pause_future = None
        enqueue = self.app.workflow.enqueue

        @contextmanager
        def observe_transaction(path):
            if threading.get_ident() == pause_thread_id:
                pause_attempted.set()
            with file_transaction(path):
                yield

        def pause():
            nonlocal pause_thread_id
            pause_thread_id = threading.get_ident()
            reply = self.app._cron(42, "/cron pause 1")
            return reply, task_queue_status(self.root).pending_count

        with ThreadPoolExecutor(max_workers=1) as executor:
            def enqueue_while_pausing(*args, **kwargs):
                nonlocal pause_future
                pause_future = executor.submit(pause)
                self.assertTrue(pause_attempted.wait(timeout=2))
                with self.assertRaises(TimeoutError):
                    pause_future.result(timeout=0.2)
                self.assertEqual(task_queue_status(self.root).pending_count, 0)
                return enqueue(*args, **kwargs)

            with (
                patch("enoch.cron._utc_now", return_value=datetime.fromisoformat(job.next_run_at)),
                patch("enoch.cron.file_transaction", side_effect=observe_transaction),
                patch.object(self.app.workflow, "enqueue", side_effect=enqueue_while_pausing),
            ):
                admitted, = self.app._enqueue_due_cron_jobs()
                reply, pending_at_pause = pause_future.result(timeout=2)
            self.assertIn("Paused cron #1.", reply)
            self.assertEqual(pending_at_pause, 1)
        paused = find_cron_job(job.id, self.root)
        self.assertEqual(paused.status, "paused")
        self.assertEqual(paused.last_task_id, admitted.id)
        self.assertFalse(paused.claim_id)
        self.assertEqual(self.app._enqueue_due_cron_jobs(), ())

    def test_cancellation_after_claim_prevents_admission(self):
        job = daily(self.root)

        def claim_then_cancel(root):
            claimed = claim_due_cron_jobs(root)
            self.assertIn("Cancelled cron #1.", self.app._cron(42, "/cron cancel 1"))
            return claimed

        with (
            patch("enoch.cron._utc_now", return_value=datetime.fromisoformat(job.next_run_at)),
            patch("enoch.app.core.claim_due_cron_jobs", side_effect=claim_then_cancel),
        ):
            self.assertEqual(self.app._enqueue_due_cron_jobs(), ())
        self.assertEqual(task_queue_status(self.root).pending_count, 0)
        self.assertEqual(find_cron_job(job.id, self.root).status, "cancelled")

    def test_stale_claim_snapshot_cannot_readmit_an_acknowledged_occurrence(self):
        job = daily(self.root)
        due = datetime.fromisoformat(job.next_run_at)
        with patch("enoch.cron._utc_now", return_value=due):
            stale = claim_due_cron_jobs(self.root)
            self.app._enqueue_due_cron_jobs()
            running = begin_next_task(self.root)
            complete_task(running.id, self.root, result="Done")
            request_cron_job_run(job.id, self.root)
            current, = claim_due_cron_jobs(self.root)
            with (
                patch("enoch.app.core.claim_due_cron_jobs", return_value=stale),
                patch.object(self.app.workflow, "enqueue", wraps=self.app.workflow.enqueue) as enqueue,
            ):
                self.assertEqual(self.app._enqueue_due_cron_jobs(), ())
                enqueue.assert_not_called()
            self.assertEqual(find_cron_job(job.id, self.root).claim_id, current.claim_id)
            admitted, = self.app._enqueue_due_cron_jobs()
            self.assertNotEqual(admitted.id, running.id)

    def test_enqueue_failure_preserves_claim_and_releases_lock_for_pause(self):
        job = daily(self.root)
        with patch("enoch.cron._utc_now", return_value=datetime.fromisoformat(job.next_run_at)):
            with patch.object(self.app.workflow, "enqueue", side_effect=OSError("queue unavailable")):
                self.assertEqual(self.app._enqueue_due_cron_jobs(), ())
            claimed = find_cron_job(job.id, self.root)
            self.assertTrue(claimed.claim_id)
            self.assertEqual(task_queue_status(self.root).pending_count, 0)
            self.assertIn("Paused cron #1.", self.app._cron(42, "/cron pause 1"))
            self.assertEqual(find_cron_job(job.id, self.root).claim_id, claimed.claim_id)
            self.app._cron(42, "/cron resume 1")
            admitted, = self.app._enqueue_due_cron_jobs()
            self.assertEqual(admitted.idempotency_key, f"cron:{job.id}:{claimed.claim_id}")

    def test_crash_after_enqueue_reuses_task_and_blocks_overlap_until_completion(self):
        job = daily(self.root)
        due = datetime(2026, 9, 16, 1, tzinfo=timezone.utc)
        with patch("enoch.cron._utc_now", return_value=due):
            with patch("enoch.app.core.record_cron_task", side_effect=OSError("crash before ack")):
                with self.assertRaises(OSError):
                    self.app._enqueue_due_cron_jobs()
            first, = task_queue_status(self.root).pending
            restarted = EnochApplication(load_identity(), self.root, self.client)
            recovered, = restarted._enqueue_due_cron_jobs()
            self.assertEqual(first.id, recovered.id)
            self.assertEqual(task_queue_status(self.root).pending_count, 1)
            request_cron_job_run(job.id, self.root, idempotency_key="manual")
            self.assertEqual(restarted._enqueue_due_cron_jobs(), ())
            running = begin_next_task(self.root)
            self.assertEqual(restarted._enqueue_due_cron_jobs(), ())
            complete_task(running.id, self.root, result="Done")
            second, = restarted._enqueue_due_cron_jobs()
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(find_cron_job(job.id, self.root).next_run_at, "2026-09-17T01:00:00+00:00")

    def test_scheduled_result_uses_original_chat_and_missing_binding_fails_explicitly(self):
        daily(self.root, chat_id=99)
        with patch("enoch.cron._utc_now", return_value=datetime(2026, 9, 16, 1, tzinfo=timezone.utc)):
            queued, = self.app._enqueue_due_cron_jobs()
        self.assertEqual(queued.chat_id, 99)
        running = begin_next_task(self.root)
        with patch.object(self.app, "_run_direct_work", return_value="Scheduled result."):
            self.app._run_task_job(running)
        self.assertTrue(any("Scheduled result." in text for _, text in self.client.sent))
        self.assertEqual({chat for chat, _ in self.client.sent}, {99})
        self.assertEqual({chat for chat, _, _ in self.client.edited}, {99})
        payload = json.loads(cron_path(self.root).read_text())
        payload["active"][0]["chat_id"] = None
        cron_path(self.root).write_text(json.dumps(payload))
        with self.assertRaisesRegex(StateCorruptionError, "bound chat"):
            self.app._enqueue_due_cron_jobs()
