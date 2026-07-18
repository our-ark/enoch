from pathlib import Path
import os
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.task_queue import (
    TaskRetryError,
    begin_direct_task,
    begin_next_task,
    cancel_task,
    cancel_running_task,
    claim_running_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    fail_task,
    pause_task,
    recover_interrupted_task,
    record_task_result,
    record_task_status_message,
    record_task_worktree,
    regress_task,
    resolve_regressed_task,
    retry_failed_task,
    retry_running_task,
    resume_paused_tasks,
    revert_task,
    task_result_has_pull_request,
    task_queue_status,
)
from enoch.task_events import load_task_events
from enoch import task_queue


class EnochTaskQueueTests(unittest.TestCase):
    def test_retry_failed_task_creates_new_linked_job_with_provenance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(
                42,
                "retry this work",
                root,
                context="Preserve this context.",
                context_source="evolve-approve",
                source="feedback",
                initiated_by="human",
                trigger="/evolve approve",
                candidate_id="feedback-1",
                evidence_source="feedback",
                signal_actor="human",
                candidate_actor="agent",
                approval_actor="human",
                parent_candidate_id="feedback-parent",
                source_task_id=7,
            )
            begin_next_task(root)
            fail_task(original.id, root, result="transient failure")

            retried = retry_failed_task(original.id, root)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=retried.id)

        self.assertEqual(retried.id, 2)
        self.assertEqual(retried.parent_task_id, original.id)
        self.assertEqual(retried.text, original.text)
        self.assertEqual(retried.context, original.context)
        self.assertEqual(retried.context_source, original.context_source)
        self.assertEqual(retried.source, "feedback")
        self.assertEqual(retried.initiated_by, "human")
        self.assertEqual(retried.trigger, "/task retry")
        self.assertEqual(retried.candidate_id, "feedback-1")
        self.assertEqual(retried.evidence_source, "feedback")
        self.assertEqual(retried.signal_actor, "human")
        self.assertEqual(retried.candidate_actor, "agent")
        self.assertEqual(retried.approval_actor, "human")
        self.assertEqual(retried.parent_candidate_id, "feedback-parent")
        self.assertEqual(retried.source_task_id, 7)
        self.assertEqual(status.history[0].status, "failed")
        self.assertEqual(status.pending, (retried,))
        self.assertEqual([event.event for event in events], ["created", "queued"])
        self.assertEqual([event.event_actor for event in events], ["human", "human"])

    def test_retry_preserves_recoverable_branch_and_reconciled_pr(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "publish existing work", root)
            running = begin_next_task(root)
            claim_running_task(running.id, "worker-one", os.getpid(), root)
            record_task_worktree(
                running.id,
                "worker-one",
                root / "task-worktree",
                "enoch/task-1",
                root,
            )
            fail_task(
                running.id,
                root,
                result="Worker stopped before recording its PR.",
                worker_id="worker-one",
            )

            retried = retry_failed_task(
                original.id,
                root,
                reconciled_result=(
                    "Existing work is available at "
                    "https://github.com/our-ark/enoch/pull/13"
                ),
            )

        self.assertEqual(retried.branch_name, "enoch/task-1")
        self.assertEqual(
            retried.worktree_path,
            str((root / "task-worktree").resolve()),
        )
        self.assertEqual(
            retried.pr_urls,
            ("https://github.com/our-ark/enoch/pull/13",),
        )
        self.assertIn("Existing work is available", retried.result)

    def test_retry_requires_latest_failed_task_in_retry_chain(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "retry chain", root)
            begin_next_task(root)
            fail_task(original.id, root, result="first failure")
            second = retry_failed_task(original.id, root)
            begin_next_task(root)
            fail_task(second.id, root, result="second failure")

            with self.assertRaisesRegex(
                TaskRetryError,
                "Retry task #2 instead",
            ):
                retry_failed_task(original.id, root)
            third = retry_failed_task(second.id, root)

        self.assertEqual(third.id, 3)
        self.assertEqual(third.parent_task_id, second.id)

    def test_retry_refuses_non_failed_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "not failed", root)

            with self.assertRaisesRegex(TaskRetryError, "not a failed task"):
                retry_failed_task(queued.id, root)

    def test_cancelled_retry_does_not_block_another_attempt(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "retry after cancellation", root)
            begin_next_task(root)
            fail_task(original.id, root, result="failed")
            cancelled_retry = retry_failed_task(original.id, root)
            cancel_task(cancelled_retry.id, root)

            next_retry = retry_failed_task(original.id, root)

        self.assertEqual(next_retry.id, 3)
        self.assertEqual(next_retry.parent_task_id, original.id)

    def test_begin_direct_task_creates_running_job_without_pending(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            running = begin_direct_task(42, "ship it now", root)
            status = task_queue_status(root)

        self.assertEqual(running.id, 1)
        self.assertEqual(running.status, "running")
        self.assertEqual(running.attempt, 1)
        self.assertEqual(running.max_attempts, 3)
        self.assertEqual(status.running, running)
        self.assertEqual(status.pending, ())

    def test_transient_failure_requeues_same_task_with_attempt_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "retry network work", root)
            running = begin_next_task(root)

            retried = retry_running_task(
                running.id,
                root,
                result="Connection reset by peer.",
                failure_code="network_error",
                failure_class="transient",
                delay_seconds=0,
            )
            second_attempt = begin_next_task(root)
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual(retried.status, "pending")
        self.assertEqual(retried.attempt, 1)
        self.assertTrue(retried.retryable)
        self.assertEqual(retried.failure_code, "network_error")
        self.assertEqual(second_attempt.id, queued.id)
        self.assertEqual(second_attempt.attempt, 2)
        self.assertFalse(second_attempt.retryable)
        self.assertEqual(
            [event.event for event in events],
            ["created", "queued", "started", "retrying", "queued", "started"],
        )
        retry_event = events[3]
        self.assertEqual(retry_event.attempt, 1)
        self.assertEqual(retry_event.max_attempts, 3)
        self.assertEqual(retry_event.failure_code, "network_error")
        self.assertEqual(retry_event.failure_class, "transient")
        self.assertTrue(retry_event.retryable)

    def test_interrupted_worker_fails_after_three_attempts(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "bounded recovery", root)

            for expected_attempt in (1, 2):
                running = begin_next_task(root)
                self.assertEqual(running.attempt, expected_attempt)
                recovered = recover_interrupted_task(root)
                self.assertEqual(recovered.status, "pending")

            running = begin_next_task(root)
            self.assertEqual(running.attempt, 3)
            failed = recover_interrupted_task(root)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.failure_code, "worker_interrupted")
        self.assertEqual(failed.failure_class, "transient")
        self.assertFalse(failed.retryable)
        self.assertEqual(status.history[-1], failed)
        self.assertEqual(
            [event.event for event in events].count("retrying"),
            2,
        )
        self.assertEqual(events[-1].event, "failed")
        self.assertEqual(events[-1].trigger, "recovery-exhausted")

    def test_legacy_evolve_task_infers_split_provenance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = task_queue.task_queue_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                """{
  "schema_version": 2,
  "next_id": 2,
  "pending": [{
    "id": 1,
    "chat_id": 42,
    "text": "apply feedback",
    "created_at": "2026-07-18T00:00:00Z",
    "source": "feedback",
    "initiated_by": "human",
    "trigger": "/evolve approve",
    "context_source": "evolve-approve",
    "candidate_id": "feedback-legacy"
  }],
  "paused": [],
  "running": null,
  "history": []
}
""",
                encoding="utf-8",
            )

            job = task_queue_status(root).pending[0]

        self.assertEqual(job.evidence_source, "feedback")
        self.assertEqual(job.signal_actor, "human")
        self.assertEqual(job.candidate_actor, "agent")
        self.assertEqual(job.approval_actor, "human")

    def test_begin_direct_task_refuses_existing_running_job(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            begin_direct_task(42, "first", root)

            with self.assertRaisesRegex(RuntimeError, "already running"):
                begin_direct_task(42, "second", root)

    def test_cancel_pending_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "first task", root)
            second = enqueue_task(42, "second task", root)

            cancelled = cancel_task(first.id, root)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=first.id)

        self.assertEqual(cancelled.id, first.id)
        self.assertEqual(status.pending, (second,))
        self.assertEqual(status.history[-1].id, first.id)
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertEqual(events[-1].event, "cancelled")
        self.assertEqual(events[-1].event_actor, "human")

    def test_pause_running_task_preserves_it_outside_terminal_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "needs Codex", root, context="Keep this context.")
            second = enqueue_task(42, "wait behind it", root)
            running = begin_next_task(root)

            paused = pause_task(
                running.id,
                root,
                result="Codex authentication is unavailable.",
            )
            status = task_queue_status(root)
            events = load_task_events(root, task_id=first.id)

        self.assertEqual(paused.id, first.id)
        self.assertEqual(paused.status, "paused")
        self.assertEqual(paused.context, "Keep this context.")
        self.assertIsNone(status.running)
        self.assertEqual(status.paused, (paused,))
        self.assertEqual(status.paused_count, 1)
        self.assertEqual(status.pending, (second,))
        self.assertEqual(status.history, ())
        self.assertEqual(events[-1].event, "paused")
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].trigger, "codex-unavailable")

    def test_resume_moves_paused_tasks_to_front_with_same_ids(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "resume me", root)
            second = enqueue_task(42, "already queued", root)
            pause_task(begin_next_task(root).id, root, result="No Codex access.")

            resumed = resume_paused_tasks(root)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=first.id)

        self.assertEqual([job.id for job in resumed], [first.id])
        self.assertEqual([job.id for job in status.pending], [first.id, second.id])
        self.assertEqual(status.paused, ())
        self.assertEqual(status.paused_count, 0)
        self.assertEqual(resumed[0].status, "pending")
        self.assertEqual([event.event for event in events[-2:]], ["paused", "resumed"])
        self.assertEqual(events[-1].event_actor, "human")
        self.assertEqual(events[-1].trigger, "/resume")

    def test_resume_can_select_one_paused_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "first paused task", root)
            second = enqueue_task(42, "second paused task", root)
            pause_task(begin_next_task(root).id, root, result="No Codex access.")
            pause_task(begin_next_task(root).id, root, result="No Codex access.")

            resumed = resume_paused_tasks(
                root,
                task_id=second.id,
                trigger="/task resume",
            )
            status = task_queue_status(root)
            events = load_task_events(root, task_id=second.id)

        self.assertEqual([job.id for job in resumed], [second.id])
        self.assertEqual([job.id for job in status.pending], [second.id])
        self.assertEqual([job.id for job in status.paused], [first.id])
        self.assertEqual(events[-1].event, "resumed")
        self.assertEqual(events[-1].trigger, "/task resume")

    def test_enqueue_task_front_runs_before_existing_pending_tasks(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "first task", root)
            second = enqueue_task(42, "second task", root)
            urgent = enqueue_task_front(
                42,
                "urgent task",
                root,
                context="Use the urgent context.",
                context_source="chat-snapshot",
            )

            status = task_queue_status(root)
            running = begin_next_task(root)

        self.assertEqual([job.id for job in status.pending], [urgent.id, first.id, second.id])
        self.assertEqual(running.id, urgent.id)
        self.assertEqual(running.context, "Use the urgent context.")
        self.assertEqual(running.context_source, "chat-snapshot")

    def test_complete_running_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            record_task_status_message(queued.id, 2001, root)
            running = begin_next_task(root)

            complete_task(running.id, root, result="Done.")
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].status_message_id, 2001)
        self.assertEqual(status.history[-1].result, "Done.")
        self.assertEqual(status.history[-1].pr_urls, ())

    def test_records_complete_lifecycle_with_source_and_actors(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(
                42,
                "adapt a brainstormed improvement",
                root,
                source="brainstorming",
                initiated_by="agent",
                event_actor="human",
                trigger="/evolve approve",
                candidate_id="brainstorm-1",
                evidence_source="brainstorming",
                signal_actor="agent",
                candidate_actor="agent",
                approval_actor="human",
            )
            running = begin_next_task(root)

            complete_task(running.id, root, result="Done.")
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual([event.event for event in events], ["created", "queued", "started", "completed"])
        self.assertEqual([event.event_actor for event in events], ["human", "human", "system", "agent"])
        self.assertTrue(all(event.source == "brainstorming" for event in events))
        self.assertTrue(all(event.initiated_by == "agent" for event in events))
        self.assertTrue(all(event.candidate_id == "brainstorm-1" for event in events))
        self.assertTrue(all(event.evidence_source == "brainstorming" for event in events))
        self.assertTrue(all(event.signal_actor == "agent" for event in events))
        self.assertTrue(all(event.candidate_actor == "agent" for event in events))
        self.assertTrue(all(event.approval_actor == "human" for event in events))

    def test_queued_task_preserves_context_snapshot_through_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(
                42,
                "do it",
                root,
                context="Build the reminders feature discussed earlier.",
                context_source="chat-snapshot",
            )

            running = begin_next_task(root)
            complete_task(running.id, root, result="Done.")
            status = task_queue_status(root)

        self.assertEqual(queued.context, "Build the reminders feature discussed earlier.")
        self.assertEqual(running.context, queued.context)
        self.assertEqual(running.context_source, "chat-snapshot")
        self.assertEqual(status.history[-1].context, queued.context)
        self.assertEqual(status.history[-1].context_source, "chat-snapshot")

    def test_direct_task_preserves_context_snapshot(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            running = begin_direct_task(
                42,
                "do it now",
                root,
                context="Use the approved high reasoning config.",
                context_source="chat-snapshot",
            )
            complete_task(running.id, root, result="Done.")
            status = task_queue_status(root)

        self.assertEqual(running.context, "Use the approved high reasoning config.")
        self.assertEqual(status.history[-1].context, running.context)
        self.assertEqual(status.history[-1].context_source, "chat-snapshot")

    def test_complete_running_task_records_pr_urls_in_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)
            result = "Opened pull request: https://github.com/our-ark/enoch/pull/3"

            complete_task(running.id, root, result=result)
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].pr_urls, ("https://github.com/our-ark/enoch/pull/3",))

    def test_enqueue_during_completion_preserves_both_updates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            running = begin_direct_task(42, "running job", root)
            original_write = task_queue._write_queue
            completion_write_started = threading.Event()
            enqueue_started = threading.Event()

            def delayed_write(data, root_arg=None):
                if threading.current_thread().name == "complete-thread":
                    completion_write_started.set()
                    self.assertTrue(enqueue_started.wait(5))
                original_write(data, root_arg)

            task_queue._write_queue = delayed_write
            try:
                complete_thread = threading.Thread(
                    target=lambda: complete_task(running.id, root, result="done"),
                    name="complete-thread",
                )
                enqueue_result = []
                enqueue_thread = threading.Thread(
                    target=lambda: (enqueue_started.set(), enqueue_result.append(enqueue_task(42, "queued", root))),
                    name="enqueue-thread",
                )

                complete_thread.start()
                self.assertTrue(completion_write_started.wait(5))
                enqueue_thread.start()
                complete_thread.join(5)
                enqueue_thread.join(5)
            finally:
                task_queue._write_queue = original_write

            status = task_queue_status(root)

        self.assertFalse(complete_thread.is_alive())
        self.assertFalse(enqueue_thread.is_alive())
        self.assertEqual([job.id for job in status.history], [running.id])
        self.assertEqual([job.id for job in status.pending], [enqueue_result[0].id])

    def test_fail_running_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)

            fail_task(running.id, root, result="GitHub rejected the push.")
            status = task_queue_status(root)
            events = load_task_events(root, task_id=queued.id)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "failed")
        self.assertEqual(status.history[-1].result, "GitHub rejected the push.")
        self.assertEqual(events[-1].event, "failed")
        self.assertEqual(events[-1].event_actor, "agent")

    def test_cancel_running_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            running = begin_direct_task(42, "stop me", root)

            cancelled = cancel_running_task(root, result="Stopped by /stop.")
            status = task_queue_status(root)
            events = load_task_events(root, task_id=running.id)

        self.assertEqual(cancelled.id, running.id)
        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, running.id)
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertEqual(status.history[-1].result, "Stopped by /stop.")
        self.assertEqual(events[-1].event, "cancelled")
        self.assertEqual(events[-1].trigger, "/stop")

    def test_recover_interrupted_task_requeues_with_status_message(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "resume me", root, context="Resume with chat context.")
            record_task_status_message(queued.id, 2001, root)
            begin_next_task(root)

            recovered = recover_interrupted_task(root)
            status = task_queue_status(root)

        self.assertEqual(recovered.id, queued.id)
        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.status_message_id, 2001)
        self.assertEqual(recovered.context, "Resume with chat context.")
        self.assertEqual(status.pending[0].id, queued.id)

    def test_recover_interrupted_task_with_pr_url_completes_it(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)
            result = "Opened pull request: https://github.com/our-ark/enoch/pull/3"
            record_task_result(running.id, result, root)

            recovered = recover_interrupted_task(root)
            status = task_queue_status(root)

        self.assertEqual(recovered.id, queued.id)
        self.assertEqual(recovered.status, "completed")
        self.assertIsNone(status.running)
        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertTrue(task_result_has_pull_request(status.history[-1].result))
        self.assertEqual(status.history[-1].pr_urls, ("https://github.com/our-ark/enoch/pull/3",))

    def test_recovery_events_are_attributed_to_system(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "resume me", root)
            begin_next_task(root)

            recover_interrupted_task(root)
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual(events[-1].event, "queued")
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].trigger, "recovery")

    def test_reverted_task_keeps_completed_regressed_and_reverted_events(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship reversible work", root)
            running = begin_next_task(root)
            complete_task(running.id, root, result="Shipped.")

            reverted = revert_task(
                queued.id,
                root,
                result="Reverted after regression.",
                event_actor="human",
                trigger="/task revert",
                related_task_id=7,
            )
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual(reverted.status, "reverted")
        self.assertEqual(
            [event.event for event in events[-3:]],
            ["completed", "regressed", "reverted"],
        )
        self.assertEqual(events[-1].event_actor, "human")
        self.assertEqual(events[-1].related_task_id, 7)

    def test_regressed_task_can_be_resolved_by_completed_forward_fix(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "ship risky work", root)
            complete_task(begin_next_task(root).id, root, result="Shipped.")
            regressed = regress_task(original.id, root, result="Broke recovery.")
            fix = enqueue_task(42, "repair recovery", root, parent_task_id=original.id)
            complete_task(begin_next_task(root).id, root, result="Recovery fixed.")

            resolved = resolve_regressed_task(
                original.id,
                "forward-fixed",
                root,
                result="Fixed by follow-up.",
                related_task_id=fix.id,
            )
            events = load_task_events(root, task_id=original.id)

        self.assertEqual(regressed.status, "regressed")
        self.assertEqual(resolved.status, "forward-fixed")
        self.assertEqual(
            [event.event for event in events[-3:]],
            ["completed", "regressed", "forward-fixed"],
        )
        self.assertEqual(events[-1].related_task_id, fix.id)

    def test_forward_fix_must_reference_another_completed_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "ship risky work", root)
            complete_task(begin_next_task(root).id, root)
            regress_task(original.id, root, result="Regression.")
            pending_fix = enqueue_task(42, "repair it", root)

            unresolved = resolve_regressed_task(
                original.id,
                "forward-fixed",
                root,
                related_task_id=pending_fix.id,
            )
            status = task_queue_status(root)

        self.assertIsNone(unresolved)
        self.assertEqual(status.history[-1].status, "regressed")

    def test_active_worker_lease_prevents_recovery_and_stale_finalization(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            running = begin_direct_task(42, "owned work", root)
            claimed = claim_running_task(running.id, "worker-one", os.getpid(), root)

            recovered = recover_interrupted_task(root)
            stale = complete_task(
                running.id,
                root,
                result="stale completion",
                worker_id="worker-two",
            )
            status = task_queue_status(root)

            self.assertIsNotNone(claimed)
            self.assertIsNone(recovered)
            self.assertIsNone(stale)
            self.assertEqual(status.running.worker_id, "worker-one")

            completed = complete_task(
                running.id,
                root,
                result="authoritative completion",
                worker_id="worker-one",
            )

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result, "authoritative completion")

    def test_dead_worker_recovery_preserves_task_worktree_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            worktree = root / "task-worktree"
            running = begin_direct_task(42, "recover work", root)
            claim_running_task(running.id, "dead-worker", 999999, root)
            record_task_worktree(
                running.id,
                "dead-worker",
                worktree,
                "enoch/task-1",
                root,
            )

            with patch("enoch.task_queue.os.kill", side_effect=ProcessLookupError):
                recovered = recover_interrupted_task(root)

            status = task_queue_status(root)

        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.worktree_path, str(worktree.resolve()))
        self.assertEqual(recovered.branch_name, "enoch/task-1")
        self.assertEqual(recovered.worker_id, "")
        self.assertIsNone(recovered.worker_pid)
        self.assertEqual(status.pending[0], recovered)


if __name__ == "__main__":
    unittest.main()
