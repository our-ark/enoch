from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from enoch.extensions import (
    AGENT_EXTENSION_API_VERSION,
    AgentExtension,
    ExtensionCommandContext,
    ExtensionLifecycleContext,
    ExtensionWorkflow,
    extension_storage,
)
from enoch.identity import load_identity
from enoch.providers import ChatEvent, RuntimeResult, TaskRequirements
from enoch.storage import local_storage_layout
from enoch.workflows import LocalWorkflowEngine
from our_ark_provider_kit import (
    BranchlessRepositoryFixture,
    IndependentReviewFixture,
)


@dataclass(frozen=True)
class ExtensionCommandCase:
    command: str
    argument: str
    expected_request: str
    expected_context: str = ""
    expected_capabilities: tuple[str, ...] = ()
    idempotency_key: str = ""


class AgentExtensionConformanceMixin:
    """Reusable checks for an independently packaged ``AgentExtension``."""

    def create_extension(self) -> AgentExtension:
        raise NotImplementedError

    def command_case(self) -> ExtensionCommandCase | None:
        return None

    def prepare_command(
        self,
        extension: AgentExtension,
        context: ExtensionCommandContext,
        case: ExtensionCommandCase,
    ) -> None:
        """Populate state required by a representative stateful command."""

        del extension, context, case

    def test_conformance_extension_uses_public_api_version(self) -> None:
        extension = self.create_extension()

        self.assertIsInstance(extension, AgentExtension)
        self.assertEqual(extension.api_version, AGENT_EXTENSION_API_VERSION)
        self.assertTrue(extension.name)
        self.assertTrue(extension.help_heading)

    def test_conformance_extension_commands_are_discoverable(self) -> None:
        extension = self.create_extension()

        for command in extension.commands:
            self.assertIs(extension.command(command.command), command)
            self.assertTrue(command.summary)

    def test_conformance_extension_storage_is_namespaced(self) -> None:
        extension = self.create_extension()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            application_storage = local_storage_layout(root)
            storage = extension_storage(application_storage, extension.name)

        self.assertEqual(storage.software_body, application_storage.software_body)
        self.assertEqual(
            storage.private_state,
            application_storage.private_path(f"extensions/{extension.name}"),
        )
        self.assertEqual(
            storage.artifacts,
            application_storage.artifact_path(f"extensions/{extension.name}"),
        )

    def test_conformance_extension_command_queues_governed_work(self) -> None:
        case = self.command_case()
        if case is None:
            return
        extension = self.create_extension()
        spec = extension.command(case.command)
        self.assertIsNotNone(spec)
        assert spec is not None

        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = LocalWorkflowEngine(root)
            context = ExtensionCommandContext(
                identity=load_identity(),
                root=root,
                storage=extension_storage(
                    local_storage_layout(root),
                    extension.name,
                ),
                conversation_id=42,
                event=ChatEvent(
                    cursor=1,
                    conversation_id=42,
                    message_id="extension-conformance",
                    text=f"{spec.command} {case.argument}".rstrip(),
                ),
                command=spec.command,
                argument=case.argument,
                runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(),
                review=IndependentReviewFixture(),
                workflow=ExtensionWorkflow.from_engine(extension.name, engine),
            )
            self.prepare_command(extension, context, case)
            response = spec.handler(context)
            pending = engine.inspect().pending

        self.assertIsInstance(response, str)
        self.assertEqual(len(pending), 1)
        job = pending[0]
        self.assertEqual(job.text, case.expected_request)
        self.assertEqual(job.context, case.expected_context)
        self.assertEqual(job.context_source, f"extension:{extension.name}")
        self.assertEqual(job.trigger, spec.command)
        self.assertEqual(job.source, "task")
        self.assertEqual(job.initiated_by, "human")
        self.assertEqual(
            job.required_capabilities,
            TaskRequirements(case.expected_capabilities).capabilities,
        )
        local_key = case.idempotency_key or "command:extension-conformance"
        self.assertEqual(
            job.idempotency_key,
            f"extension:{extension.name}:{local_key}",
        )

    def test_conformance_extension_lifecycle_accepts_isolated_context(self) -> None:
        extension = self.create_extension()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = LocalWorkflowEngine(root)
            context = ExtensionLifecycleContext(
                identity=load_identity(),
                root=root,
                storage=extension_storage(
                    local_storage_layout(root),
                    extension.name,
                ),
                chat=_Chat(),
                runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(),
                review=IndependentReviewFixture(),
                workflow=ExtensionWorkflow.from_engine(extension.name, engine),
            )
            for hook in (
                extension.lifecycle.on_initialize,
                extension.lifecycle.on_startup,
                extension.lifecycle.before_run,
                extension.lifecycle.after_run,
                extension.lifecycle.on_shutdown,
            ):
                if hook is not None:
                    hook(context)


class _Chat:
    name = "extension-conformance-chat"
    provider_kind = "chat"
    allowed_conversation_id = 42

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        return 1

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        return None


class _Runtime:
    name = "extension-conformance-runtime"
    provider_kind = "runtime"
    config_section = "extension-conformance"

    def respond(self, identity, message, **kwargs):
        return RuntimeResult(final_text="response")

    def act_in_session(self, identity, message, **kwargs):
        return RuntimeResult(final_text="action")

    def model_summary(self, root=None):
        return "extension conformance runtime"

    def model_options(self):
        return ()

    def reset_usage(self):
        return None

    def health(self, root=None):
        return None
