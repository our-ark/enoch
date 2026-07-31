from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.app.core import EnochApplication
from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.extensions import (
    AGENT_EXTENSION_API_VERSION,
    EXTENSION_COMMAND_RESULT_API_VERSION,
    ExtensionArtifactReference,
    ExtensionCommandResult,
    ExtensionCommandSpec,
    ExtensionTaskEvent,
    ExtensionWorkflow,
    ExtensionWorkflowCapabilityError,
    ExtensionWorkflowControlError,
    AgentExtension,
    AgentExtensionError,
    ExtensionLifecycleHooks,
    load_extensions,
    register_extension,
)
from enoch.extensions.events import load_extension_task_event_receipts
from enoch.extensions import registry as extension_registry
from enoch.identity import load_identity
from enoch.profiles import AgentProfile, CommandSpec, LifecycleHooks
from enoch.providers import (
    AgentRuntimeAccessUnavailable,
    ChatEvent,
    ProviderHealth,
)
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import task_queue_status
from enoch.workflows import (
    WORKFLOW_FEATURE_ARTIFACT_REFERENCES,
    WORKFLOW_FEATURE_EXECUTION_LANES,
    WORKFLOW_FEATURE_STRUCTURED_METADATA,
    LocalWorkflowEngine,
)


class _Chat:
    name = "extension-chat"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    @property
    def allowed_conversation_id(self):
        return "room-1"

    def receive(self, cursor=None):
        return ()

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "message-1"

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        return None


class _Runtime:
    name = "extension-runtime"
    provider_kind = "runtime"
    config_section = "extension-runtime"

    def respond(self, identity, message, **kwargs):
        return "extension response"

    def act_in_session(self, identity, message, **kwargs):
        return "extension task response"

    def model_summary(self, root=None):
        return "AI model: extension-runtime"

    def model_options(self):
        return ()

    def reset_usage(self):
        return None

    def health(self, root=None):
        return ProviderHealth("extension runtime", True, "extension doctor", "ready")


class _UnlockedChat(_Chat):
    @property
    def allowed_conversation_id(self):
        return None


class _EntryPoint:
    name = "manager"

    def load(self):
        return lambda _root=None: AgentExtension(name="manager")


class _EntryPoints(list):
    def select(self, *, group):
        return self if group == "our_ark.extensions" else ()


class EnochExtensionTests(unittest.TestCase):
    def test_extension_command_uses_namespaced_storage_and_shared_workflow(self) -> None:
        storage_layouts = []

        def plan(context):
            storage_layouts.append(context.storage)
            state = context.storage.private_path("projects.json")
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("[]\n", encoding="utf-8")
            job = context.enqueue_task(
                f"Plan {context.argument}",
                context="Build a dependency graph.",
            )
            return f"Queued planning task #{job.id}."

        extension = AgentExtension(
            name="manager",
            help_heading="Communication & collaboration",
            commands=(
                ExtensionCommandSpec(
                    "project",
                    "manage a project graph",
                    plan,
                    usage="/project <goal> - create a project plan",
                ),
            ),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                root,
                chat,
                runtime=_Runtime(),
                extensions=(extension,),
            )

            app.handle_event(_event("/project prepare the launch"))

            queued = task_queue_status(root).pending
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].text, "Plan prepare the launch")
            self.assertEqual(queued[0].context, "Build a dependency graph.")
            self.assertEqual(queued[0].context_source, "extension:manager")
            self.assertEqual(queued[0].trigger, "/project")
            self.assertEqual(
                queued[0].idempotency_key,
                "extension:manager:command:message-1",
            )
            events = load_task_events(root, task_id=queued[0].id)
            self.assertEqual(events[0].event_actor, "human")
            self.assertEqual(
                storage_layouts[0].private_state,
                root.resolve() / ".enoch" / "extensions" / "manager",
            )
            self.assertEqual(
                storage_layouts[0].artifacts,
                root.resolve() / ".enoch" / "artifacts" / "extensions" / "manager",
            )
            self.assertTrue(
                storage_layouts[0].private_path("projects.json").is_file()
            )
            self.assertEqual(chat.sent[-1][1], "Queued planning task #1.")

            app.handle_event(_event("/help", message_id="help"))
            self.assertIn("Communication & collaboration:", chat.sent[-1][1])
            self.assertIn("/project - manage a project graph", chat.sent[-1][1])

            app.handle_event(_event("/help project", message_id="help-project"))
            self.assertEqual(
                chat.sent[-1][1],
                "/project <goal> - create a project plan",
            )

    def test_extension_lifecycle_wraps_run_and_unwinds_in_reverse(self) -> None:
        events: list[str] = []

        def extension(name: str) -> AgentExtension:
            return AgentExtension(
                name=name,
                lifecycle=ExtensionLifecycleHooks(
                    on_initialize=lambda context: events.append(
                        f"initialize:{name}:{context.workflow.extension_name}"
                    ),
                    before_run=lambda _context: events.append(f"before:{name}"),
                    after_run=lambda _context: events.append(f"after:{name}"),
                ),
            )

        with TemporaryDirectory() as temp:
            app = EnochApplication(
                load_identity(),
                Path(temp),
                _Chat(),
                runtime=_Runtime(),
                extensions=(extension("one"), extension("two")),
            )
            with patch.object(app, "_maybe_start_task_worker"):
                app.run_once()

        self.assertEqual(
            events,
            [
                "initialize:one:one",
                "initialize:two:two",
                "before:one",
                "before:two",
                "after:two",
                "after:one",
            ],
        )

    def test_startup_hooks_run_once_without_a_chat_lock(self) -> None:
        events: list[str] = []
        profile = AgentProfile(
            name="startup-profile",
            lifecycle=LifecycleHooks(
                on_startup=lambda _context: events.append("profile")
            ),
        )
        extension = AgentExtension(
            name="startup-extension",
            lifecycle=ExtensionLifecycleHooks(
                on_startup=lambda _context: events.append("extension")
            ),
        )
        with TemporaryDirectory() as temp:
            app = EnochApplication(
                load_identity(),
                Path(temp),
                _UnlockedChat(),
                runtime=_Runtime(),
                profile=profile,
                extensions=(extension,),
            )

            app.start()
            app.start()
            app.notify_startup()

        self.assertEqual(events, ["profile", "extension"])

    def test_extension_command_can_idempotently_enqueue_multiple_tasks(self) -> None:
        def fanout(context):
            first = context.enqueue_task(
                "Create the slides",
                idempotency_key="project:one:slides",
            )
            second = context.enqueue_task(
                "Render the video",
                idempotency_key="project:one:video",
            )
            return f"Tasks #{first.id} and #{second.id}."

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec(
                    "fanout",
                    "queue two project nodes",
                    fanout,
                ),
            ),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            app = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=_Runtime(),
                extensions=(extension,),
            )

            app.handle_event(_event("/fanout", message_id="fanout-one"))
            app.handle_event(_event("/fanout", message_id="fanout-retry"))
            pending = task_queue_status(root).pending

        self.assertEqual(tuple(job.id for job in pending), (1, 2))
        self.assertEqual(
            tuple(job.idempotency_key for job in pending),
            (
                "extension:manager:project:one:slides",
                "extension:manager:project:one:video",
            ),
        )
        self.assertEqual(
            tuple(job.context_source for job in pending),
            ("extension:manager", "extension:manager"),
        )

    def test_extension_workflow_controls_owned_task_lifecycle(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            engine = LocalWorkflowEngine(root)
            workflow = ExtensionWorkflow.from_engine("manager", engine)

            pending = workflow.enqueue("room-1", "Cancel pending work")
            cancelled = workflow.cancel(pending.id)
            cancelled_again = workflow.cancel(pending.id)

            failed = workflow.enqueue("room-1", "Retry failed work")
            engine.start_next()
            engine.finalize(
                failed.id,
                "failed",
                result="temporary outage",
                failure_code="service_unavailable",
                failure_class="transient",
                retryable=True,
            )
            retried = workflow.retry(failed.id)

            rerun = workflow.rerun(
                cancelled.task_id,
                idempotency_key="cancelled-task-v1",
            )
            restarted = ExtensionWorkflow.from_engine(
                "manager",
                LocalWorkflowEngine(root),
            )
            same_rerun = restarted.rerun(
                cancelled.task_id,
                idempotency_key="cancelled-task-v1",
            )
            events = load_task_events(root)

        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(cancelled_again, cancelled)
        self.assertEqual(retried.state, "pending")
        self.assertEqual(retried.parent_task_id, failed.id)
        self.assertEqual(rerun.state, "pending")
        self.assertEqual(rerun.parent_task_id, pending.id)
        self.assertEqual(same_rerun.task_id, rerun.task_id)
        self.assertEqual(
            rerun.idempotency_key,
            "extension:manager:rerun:cancelled-task-v1",
        )
        self.assertTrue(
            any(
                event.task_id == pending.id and event.event == "cancelled"
                for event in events
            )
        )

    def test_extension_workflow_round_trips_structured_request_data(self) -> None:
        metadata = {
            "project_id": "project-17",
            "revision": 3,
            "policy": {"review": True, "owners": ["gary", "enoch"]},
        }
        artifact_refs = (
            ExtensionArtifactReference(
                "project-spec",
                "projects/project-17/spec.md",
                "text/markdown",
            ),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            engine = LocalWorkflowEngine(root)
            workflow = ExtensionWorkflow.from_engine("manager", engine)
            queued = workflow.enqueue(
                "room-1",
                "Implement the project specification",
                metadata=metadata,
                artifact_refs=artifact_refs,
            )

            restarted = ExtensionWorkflow.from_engine(
                "manager",
                LocalWorkflowEngine(root),
            )
            status = restarted.status(queued.id)
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual(
            workflow.features,
            frozenset(
                {
                    WORKFLOW_FEATURE_ARTIFACT_REFERENCES,
                    WORKFLOW_FEATURE_STRUCTURED_METADATA,
                }
            ),
        )
        self.assertFalse(workflow.supports(WORKFLOW_FEATURE_EXECUTION_LANES))
        self.assertEqual(status.metadata, metadata)
        self.assertEqual(status.artifact_refs, artifact_refs)
        self.assertEqual(events[-1].extension_metadata, metadata)
        self.assertEqual(events[-1].extension_artifact_refs, artifact_refs)

    def test_extension_retry_preserves_and_rerun_can_replace_request_data(
        self,
    ) -> None:
        original_ref = ExtensionArtifactReference(
            "input",
            "projects/original.json",
            "application/json",
        )
        replacement_ref = ExtensionArtifactReference(
            "input",
            "projects/replacement.json",
            "application/json",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            engine = LocalWorkflowEngine(root)
            workflow = ExtensionWorkflow.from_engine("manager", engine)
            original = workflow.enqueue(
                "room-1",
                "Run the project workflow",
                metadata={"project_id": "original"},
                artifact_refs=(original_ref,),
            )
            engine.start_next()
            engine.finalize(
                original.id,
                "failed",
                failure_code="service_unavailable",
                failure_class="transient",
                retryable=True,
            )

            retry = workflow.retry(original.id)
            preserved_rerun = workflow.rerun(
                original.id,
                idempotency_key="preserved-v1",
            )
            rerun = workflow.rerun(
                original.id,
                idempotency_key="replacement-v1",
                metadata={"project_id": "replacement"},
                artifact_refs=(replacement_ref,),
            )
            restarted = ExtensionWorkflow.from_engine(
                "manager",
                LocalWorkflowEngine(root),
            )
            retry_status = restarted.status(retry.task_id)
            preserved_rerun_status = restarted.status(preserved_rerun.task_id)
            rerun_status = restarted.status(rerun.task_id)

        self.assertEqual(retry_status.metadata, {"project_id": "original"})
        self.assertEqual(retry_status.artifact_refs, (original_ref,))
        self.assertEqual(
            preserved_rerun_status.metadata,
            {"project_id": "original"},
        )
        self.assertEqual(
            preserved_rerun_status.artifact_refs,
            (original_ref,),
        )
        self.assertEqual(
            rerun_status.metadata,
            {"project_id": "replacement"},
        )
        self.assertEqual(
            rerun_status.artifact_refs,
            (replacement_ref,),
        )

    def test_extension_request_data_is_validated_before_enqueue(self) -> None:
        invalid_metadata = (
            {"_enoch": "reserved"},
            {"Project_ID": "not-lowercase"},
            {"project": object()},
            {"score": float("nan")},
            {"project": [[[[["too deep"]]]]]},
            {"project": "x" * 2049},
            {f"field_{index}": "x" * 1900 for index in range(9)},
        )
        with TemporaryDirectory() as temp:
            engine = LocalWorkflowEngine(Path(temp))
            workflow = ExtensionWorkflow.from_engine("manager", engine)

            for metadata in invalid_metadata:
                with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                    workflow.enqueue("room-1", "Do work", metadata=metadata)
            for path in (
                "../researcher/private.json",
                "/tmp/outside.json",
                "extensions/researcher/private.json",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    ExtensionArtifactReference("input", path)
            with self.assertRaises(ValueError):
                workflow.enqueue(
                    "room-1",
                    "Reject an untyped artifact",
                    artifact_refs=("projects/raw.txt",),  # type: ignore[arg-type]
                )

            pending = engine.inspect().pending

        self.assertEqual(pending, ())

    def test_extension_request_data_requires_declared_workflow_features(self) -> None:
        class LegacyWorkflow(LocalWorkflowEngine):
            features = frozenset()

        with TemporaryDirectory() as temp:
            engine = LegacyWorkflow(Path(temp))
            workflow = ExtensionWorkflow.from_engine("manager", engine)

            with self.assertRaises(ExtensionWorkflowCapabilityError) as raised:
                workflow.enqueue(
                    "room-1",
                    "Do structured work",
                    metadata={"project_id": "project-17"},
                )

        self.assertEqual(
            raised.exception.feature,
            WORKFLOW_FEATURE_STRUCTURED_METADATA,
        )
        self.assertEqual(engine.inspect().pending, ())

    def test_extension_workflow_rejects_unowned_tasks_before_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            engine = LocalWorkflowEngine(root)
            manager = ExtensionWorkflow.from_engine("manager", engine)
            researcher = ExtensionWorkflow.from_engine("researcher", engine)
            core = engine.enqueue("room-1", "Core task")
            peer = researcher.enqueue("room-1", "Peer task")

            for task_id in (core.id, peer.id):
                with self.subTest(task_id=task_id), self.assertRaises(
                    ExtensionWorkflowControlError,
                ) as raised:
                    manager.cancel(task_id)
                self.assertEqual(raised.exception.code, "task_not_owned")

            status = engine.inspect()

        self.assertEqual(
            tuple(job.id for job in status.pending),
            (core.id, peer.id),
        )

    def test_extension_workflow_enforces_terminal_and_retry_rules(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            engine = LocalWorkflowEngine(root)
            workflow = ExtensionWorkflow.from_engine("manager", engine)
            pending = workflow.enqueue("room-1", "Pending task")

            with self.assertRaises(ExtensionWorkflowControlError) as rerun_error:
                workflow.rerun(pending.id, idempotency_key="too-early")

            engine.start_next()
            engine.finalize(
                pending.id,
                "failed",
                result="permanent failure",
                failure_code="validation_failed",
                failure_class="permanent",
                retryable=False,
            )
            with self.assertRaises(ExtensionWorkflowControlError) as retry_error:
                workflow.retry(pending.id)
            with self.assertRaises(ExtensionWorkflowControlError) as key_error:
                workflow.rerun(pending.id, idempotency_key="")

        self.assertEqual(rerun_error.exception.code, "invalid_state")
        self.assertEqual(retry_error.exception.code, "not_retryable")
        self.assertEqual(key_error.exception.code, "idempotency_required")

    def test_extension_running_cancel_signals_worker_and_is_epoch_fenced(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first_epoch = begin_daemon_epoch(root)
            engine = LocalWorkflowEngine(root, epoch=first_epoch)
            cancellation = threading.Event()
            workflow = ExtensionWorkflow.from_engine(
                "manager",
                engine,
                request_running_cancellation=lambda _task_id: cancellation.set(),
            )
            running = workflow.enqueue("room-1", "Running task")
            engine.start_next()
            workflow.cancel(running.id)

            stale_engine = LocalWorkflowEngine(root, epoch=first_epoch)
            stale = ExtensionWorkflow.from_engine("manager", stale_engine)
            pending = ExtensionWorkflow.from_engine(
                "manager",
                LocalWorkflowEngine(root),
            ).enqueue("room-1", "Fenced task")
            begin_daemon_epoch(root)
            with self.assertRaises(StaleDaemonEpoch):
                stale.cancel(pending.id)
            recovered = ExtensionWorkflow.from_engine(
                "manager",
                LocalWorkflowEngine(root),
            ).status(pending.id)

        self.assertTrue(cancellation.is_set())
        self.assertEqual(recovered.state, "pending")

    def test_typed_extension_command_result_links_durable_work_and_audit_refs(
        self,
    ) -> None:
        def plan(context):
            job = context.enqueue_task("Create the governed plan")
            return ExtensionCommandResult.success(
                f"Queued planning task #{job.id}.",
                code="plan_queued",
                task_ids=(job.id,),
                output_refs=("artifact://plans/project-1",),
            )

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec("project", "queue a project plan", plan),
            ),
        )
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._record_system_event"
        ) as record_event:
            root = Path(temp)
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                root,
                chat,
                runtime=_Runtime(),
                extensions=(extension,),
            )

            app.handle_event(_event("/project"))
            queued = task_queue_status(root).pending

        self.assertEqual(chat.sent[-1][1], "Queued planning task #1.")
        self.assertEqual(tuple(job.id for job in queued), (1,))
        result_event = next(
            call
            for call in record_event.call_args_list
            if call.args[0] == "agent_extension_command_result"
        )
        self.assertEqual(result_event.kwargs["status"], "ok")
        self.assertEqual(
            result_event.kwargs["details"],
            {
                "extension": "manager",
                "command": "/project",
                "result_api_version": EXTENSION_COMMAND_RESULT_API_VERSION,
                "result_status": "succeeded",
                "code": "plan_queued",
                "task_ids": [1],
                "output_refs": ["artifact://plans/project-1"],
            },
        )

    def test_string_extension_command_result_remains_text_shorthand(self) -> None:
        extension = AgentExtension(
            name="legacy",
            commands=(
                ExtensionCommandSpec(
                    "ready",
                    "return legacy text",
                    lambda _context: "  Ready.  ",
                ),
            ),
        )
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._record_system_event"
        ) as record_event:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                extensions=(extension,),
            )

            app.handle_event(_event("/ready"))

        self.assertEqual(chat.sent[-1][1], "  Ready.  ")
        result_event = next(
            call
            for call in record_event.call_args_list
            if call.args[0] == "agent_extension_command_result"
        )
        self.assertEqual(result_event.kwargs["details"]["code"], "ok")
        self.assertEqual(
            result_event.kwargs["details"]["result_status"],
            "succeeded",
        )

    def test_extension_command_result_rejects_invalid_typed_fields(self) -> None:
        cases = (
            (
                "API version",
                lambda: ExtensionCommandResult(
                    "Ready.",
                    api_version=EXTENSION_COMMAND_RESULT_API_VERSION + 1,
                ),
            ),
            (
                "status",
                lambda: ExtensionCommandResult("Ready.", status="unknown"),
            ),
            (
                "non-ok code",
                lambda: ExtensionCommandResult("Failed.", status="failed"),
            ),
            (
                "positive integers",
                lambda: ExtensionCommandResult("Ready.", task_ids=(0,)),
            ),
            (
                "output references",
                lambda: ExtensionCommandResult(
                    "Ready.",
                    output_refs=("artifact://one\nartifact://two",),
                ),
            ),
        )

        for message, create_result in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                AgentExtensionError,
                message,
            ):
                create_result()

    def test_extension_command_failures_have_typed_outcomes(self) -> None:
        def explicit_failure(_context):
            return ExtensionCommandResult.failure(
                "A project goal is required.",
                code="missing_goal",
            )

        def validation_failure(_context):
            raise ValueError("project goal is required")

        def enqueue_failure(context):
            context.enqueue_task("")
            return "unreachable"

        def unavailable(_context):
            raise AgentRuntimeAccessUnavailable("runtime is offline")

        def internal_failure(_context):
            raise RuntimeError("secret implementation detail")

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec("explicit", "typed failure", explicit_failure),
                ExtensionCommandSpec("validate", "invalid input", validation_failure),
                ExtensionCommandSpec("enqueue", "failed enqueue", enqueue_failure),
                ExtensionCommandSpec(
                    "authorize",
                    "denied capability",
                    lambda _context: "unreachable",
                    required_capabilities=("runtime.admin",),
                ),
                ExtensionCommandSpec("unavailable", "missing runtime", unavailable),
                ExtensionCommandSpec(
                    "explode",
                    "isolated exception",
                    internal_failure,
                ),
            ),
        )
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._record_system_event"
        ) as record_event:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                extensions=(extension,),
            )

            for index, command in enumerate(
                (
                    "/explicit",
                    "/validate",
                    "/enqueue",
                    "/authorize",
                    "/unavailable",
                    "/explode",
                ),
                start=1,
            ):
                app.handle_event(_event(command, message_id=f"failure-{index}"))

        result_calls = [
            call
            for call in record_event.call_args_list
            if call.args[0] == "agent_extension_command_result"
        ]
        self.assertEqual(
            tuple(call.kwargs["details"]["code"] for call in result_calls),
            (
                "missing_goal",
                "validation_failed",
                "enqueue_failed",
                "authorization_denied",
                "capability_unavailable",
                "internal_failure",
            ),
        )
        self.assertTrue(
            all(call.kwargs["status"] == "failed" for call in result_calls)
        )
        self.assertEqual(chat.sent[0][1], "A project goal is required.")
        self.assertNotIn("secret implementation detail", chat.sent[-1][1])
        self.assertEqual(chat.sent[-1][1], "Extension command /explode failed.")

    def test_extension_command_reports_missing_workflow_feature(self) -> None:
        class LegacyWorkflow(LocalWorkflowEngine):
            features = frozenset()

        def queue(context):
            context.enqueue_task(
                "Queue structured work",
                metadata={"project_id": "project-17"},
            )
            return "unreachable"

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec("project", "queue project work", queue),
            ),
        )
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._record_system_event"
        ) as record_event:
            root = Path(temp)
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                root,
                chat,
                runtime=_Runtime(),
                workflow=LegacyWorkflow(root),
                extensions=(extension,),
            )

            app.handle_event(_event("/project"))

        result = next(
            call
            for call in record_event.call_args_list
            if call.args[0] == "agent_extension_command_result"
        )
        self.assertEqual(
            result.kwargs["details"]["code"],
            "workflow_capability_unavailable",
        )
        self.assertIn("does not support", chat.sent[-1][1])
        self.assertEqual(app.workflow.inspect().pending, ())

    def test_status_reports_active_extension_api_versions(self) -> None:
        extension = AgentExtension(name="manager")
        with TemporaryDirectory() as temp:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                extensions=(extension,),
            )

            app.handle_event(_event("/status"))

        self.assertIn(
            "Agent extensions: manager (API v1)",
            chat.sent[-1][1],
        )

    def test_extension_receives_durable_task_events_once_in_order(self) -> None:
        delivered: list[ExtensionTaskEvent] = []

        def queue(context):
            job = context.enqueue_task(
                "Create the project artifact",
                metadata={"project_id": "project-17"},
                artifact_refs=(
                    ExtensionArtifactReference(
                        "project-spec",
                        "projects/project-17/spec.md",
                        "text/markdown",
                    ),
                ),
            )
            return f"Queued task #{job.id}."

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec("project", "queue project work", queue),
            ),
            lifecycle=ExtensionLifecycleHooks(
                on_task_event=lambda _context, event: delivered.append(event),
            ),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            app = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=_Runtime(),
                extensions=(extension,),
            )
            app.handle_event(_event("/project"))
            app.start()
            running = app.workflow.start_next()
            assert running is not None
            app.workflow.finalize(
                running.id,
                "completed",
                result="Created artifact.txt",
            )
            with patch.object(app, "_maybe_start_task_worker"):
                app.run_once()
                app.run_once()
            receipts = load_extension_task_event_receipts(
                app._extension_lifecycle_context(extension).storage
            )

        self.assertEqual(
            tuple(event.event for event in delivered),
            ("queued", "started", "completed"),
        )
        self.assertTrue(
            all(event.extension_name == "manager" for event in delivered)
        )
        self.assertEqual(delivered[-1].result_summary, "Created artifact.txt")
        self.assertEqual(delivered[-1].metadata, {"project_id": "project-17"})
        self.assertEqual(
            delivered[-1].artifact_refs,
            (
                ExtensionArtifactReference(
                    "project-spec",
                    "projects/project-17/spec.md",
                    "text/markdown",
                ),
            ),
        )
        self.assertEqual(len(receipts), 3)
        self.assertEqual(
            delivered[-1].delivery_key,
            f"extension:manager:task-event:{delivered[-1].id}",
        )

    def test_extension_cancel_delivers_its_durable_task_event(self) -> None:
        delivered: list[ExtensionTaskEvent] = []

        def queue(context):
            job = context.enqueue_task("Cancel this project task")
            return f"Queued task #{job.id}."

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec("project", "queue project work", queue),
            ),
            lifecycle=ExtensionLifecycleHooks(
                on_task_event=lambda _context, event: delivered.append(event),
            ),
        )
        with TemporaryDirectory() as temp:
            app = EnochApplication(
                load_identity(),
                Path(temp),
                _Chat(),
                runtime=_Runtime(),
                extensions=(extension,),
            )
            app.handle_event(_event("/project"))
            task_id = app.workflow.inspect().pending[0].id
            app._extension_workflow(extension).cancel(task_id)
            app.start()

        self.assertEqual(
            tuple((event.task_id, event.event) for event in delivered),
            ((task_id, "queued"), (task_id, "cancelled")),
        )

    def test_failed_extension_task_event_is_replayed_after_restart(self) -> None:
        attempts: list[str] = []
        fail = True

        def queue(context):
            context.enqueue_task("Retry event delivery")
            return "Queued."

        def receive(_context, event):
            nonlocal fail
            attempts.append(event.id)
            if fail:
                fail = False
                raise RuntimeError("injected delivery failure")

        extension = AgentExtension(
            name="manager",
            commands=(
                ExtensionCommandSpec("project", "queue project work", queue),
            ),
            lifecycle=ExtensionLifecycleHooks(on_task_event=receive),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=_Runtime(),
                extensions=(extension,),
            )
            first.handle_event(_event("/project"))
            first.start()

            restarted = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=_Runtime(),
                extensions=(extension,),
            )
            restarted.start()
            restarted.start()
            receipts = load_extension_task_event_receipts(
                restarted._extension_lifecycle_context(extension).storage
            )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(receipts, frozenset({attempts[0]}))

    def test_extension_cannot_shadow_core_profile_or_peer_commands(self) -> None:
        cases = (
            (
                AgentProfile(name="enoch"),
                (
                    AgentExtension(
                        name="manager",
                        commands=(
                            ExtensionCommandSpec("task", "shadow core", lambda _: "no"),
                        ),
                    ),
                ),
                "/task",
            ),
            (
                AgentProfile(
                    name="researcher",
                    commands=(
                        CommandSpec("research", "research", lambda _: "ready"),
                    ),
                ),
                (
                    AgentExtension(
                        name="manager",
                        commands=(
                            ExtensionCommandSpec(
                                "research",
                                "shadow profile",
                                lambda _: "no",
                            ),
                        ),
                    ),
                ),
                "/research",
            ),
            (
                AgentProfile(name="enoch"),
                (
                    AgentExtension(
                        name="one",
                        commands=(
                            ExtensionCommandSpec("project", "first", lambda _: "one"),
                        ),
                    ),
                    AgentExtension(
                        name="two",
                        commands=(
                            ExtensionCommandSpec("project", "second", lambda _: "two"),
                        ),
                    ),
                ),
                "/project",
            ),
        )
        for profile, extensions, command in cases:
            with self.subTest(command=command), TemporaryDirectory() as temp:
                with self.assertRaisesRegex(
                    AgentExtensionError,
                    f"registered commands: {command}",
                ):
                    EnochApplication(
                        load_identity(),
                        Path(temp),
                        _Chat(),
                        runtime=_Runtime(),
                        profile=profile,
                        extensions=extensions,
                    )

    def test_extension_registry_supports_static_entry_point_and_config(self) -> None:
        with patch.dict(extension_registry._REGISTERED, {}, clear=True), patch.object(
            extension_registry,
            "_entry_points",
            return_value=_EntryPoints([_EntryPoint()]),
        ):
            register_extension(
                "local",
                lambda _root=None: AgentExtension(name="local"),
            )

            self.assertEqual(
                tuple(extension.name for extension in load_extensions(names=("local",))),
                ("local",),
            )
            self.assertEqual(
                tuple(
                    extension.name
                    for extension in load_extensions(names=("manager",))
                ),
                ("manager",),
            )

            with TemporaryDirectory() as temp:
                root = Path(temp)
                config = root / ".enoch" / "config.yaml"
                config.parent.mkdir()
                config.write_text(
                    "agent:\n  extensions: local, manager\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    tuple(
                        extension.name
                        for extension in load_extensions(root)
                    ),
                    ("local", "manager"),
                )

    def test_extension_rejects_unsupported_api_and_duplicate_names(self) -> None:
        with self.assertRaisesRegex(
            AgentExtensionError,
            f"supports version {AGENT_EXTENSION_API_VERSION}",
        ):
            AgentExtension(
                name="future",
                api_version=AGENT_EXTENSION_API_VERSION + 1,
            )

        extension = AgentExtension(name="manager")
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(
                AgentExtensionError,
                "Duplicate agent extension",
            ):
                EnochApplication(
                    load_identity(),
                    Path(temp),
                    _Chat(),
                    runtime=_Runtime(),
                    extensions=(extension, extension),
                )

    def test_extension_failures_are_isolated_and_audited(self) -> None:
        def fail_command(_context):
            raise RuntimeError("command exploded")

        def fail_hook(_context):
            raise RuntimeError("hook exploded")

        extension = AgentExtension(
            name="faulty",
            commands=(
                ExtensionCommandSpec(
                    "fault",
                    "exercise failure isolation",
                    fail_command,
                ),
            ),
            lifecycle=ExtensionLifecycleHooks(on_initialize=fail_hook),
        )
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._record_system_event"
        ) as record_event:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                extensions=(extension,),
            )
            app.handle_event(_event("/fault"))

        self.assertEqual(
            chat.sent[-1][1],
            "Extension command /fault failed.",
        )
        events = [call.args[0] for call in record_event.call_args_list]
        self.assertIn("agent_extension_lifecycle_failed", events)
        self.assertIn("agent_extension_command_failed", events)
        self.assertIn("agent_extension_command_result", events)


def _event(text: str, *, message_id: str = "message-1") -> ChatEvent:
    return ChatEvent(
        cursor=message_id,
        conversation_id="room-1",
        message_id=message_id,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
