from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from enoch.identity import load_identity
from enoch.profiles import (
    PROFILE_API_VERSION,
    AgentProfile,
    CommandContext,
    LifecycleContext,
    PromptContext,
)
from enoch.profiles.contracts import extend_prompt
from enoch.providers import ChatEvent, RuntimeResult, TaskRequirements
from enoch.storage import local_storage_layout
from enoch.tasks.queue import TaskJob
from our_ark_provider_kit import (
    BranchlessRepositoryFixture,
    IndependentReviewFixture,
)


@dataclass(frozen=True)
class ProfileCommandCase:
    command: str
    argument: str
    expected_request: str
    expected_context: str = ""
    expected_capabilities: tuple[str, ...] = ()


class ProfileConformanceMixin:
    """Reusable checks for a downstream ``AgentProfile`` package."""

    def create_profile(self) -> AgentProfile:
        raise NotImplementedError

    def command_case(self) -> ProfileCommandCase | None:
        return None

    def test_conformance_profile_uses_public_api_version(self) -> None:
        profile = self.create_profile()

        self.assertIsInstance(profile, AgentProfile)
        self.assertEqual(profile.api_version, PROFILE_API_VERSION)
        self.assertTrue(profile.name)
        self.assertTrue(profile.display_name)
        self.assertTrue(profile.help_heading)

    def test_conformance_profile_commands_are_discoverable(self) -> None:
        profile = self.create_profile()

        for command in profile.commands:
            self.assertIs(profile.command(command.command), command)
            self.assertTrue(command.summary)

    def test_conformance_profile_command_queues_governed_work(self) -> None:
        case = self.command_case()
        if case is None:
            return
        profile = self.create_profile()
        spec = profile.command(case.command)
        self.assertIsNotNone(spec)
        assert spec is not None
        queued: list[tuple[str, str, TaskRequirements]] = []

        def enqueue(
            request: str,
            context: str,
            requirements: TaskRequirements,
        ) -> TaskJob:
            queued.append((request, context, requirements))
            return _task(request)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = CommandContext(
                identity=load_identity(),
                root=root,
                storage=local_storage_layout(root),
                conversation_id=42,
                event=ChatEvent(
                    cursor=1,
                    conversation_id=42,
                    message_id=1,
                    text=f"/{spec.name} {case.argument}".rstrip(),
                ),
                command=spec.name,
                argument=case.argument,
                runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(),
                review=IndependentReviewFixture(),
                _enqueue=enqueue,
            )
            response = spec.handler(context)

        self.assertIsInstance(response, str)
        self.assertEqual(len(queued), 1)
        request, task_context, requirements = queued[0]
        self.assertEqual(request, case.expected_request)
        self.assertEqual(task_context, case.expected_context)
        self.assertEqual(
            requirements.capabilities,
            TaskRequirements(case.expected_capabilities).capabilities,
        )

    def test_conformance_profile_prompt_contributors_are_bounded(self) -> None:
        profile = self.create_profile()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = "Base prompt"
            extended = extend_prompt(
                base,
                profile,
                PromptContext(
                    identity=load_identity(),
                    root=root,
                    storage=local_storage_layout(root),
                    purpose="task",
                    conversation_id=42,
                    prompt=base,
                ),
            )

        self.assertTrue(extended.startswith(base))

    def test_conformance_profile_lifecycle_accepts_isolated_context(self) -> None:
        profile = self.create_profile()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = LifecycleContext(
                identity=load_identity(),
                root=root,
                storage=local_storage_layout(root),
                chat=_Chat(),
                runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(),
                review=IndependentReviewFixture(),
            )
            for hook in (
                profile.lifecycle.on_initialize,
                profile.lifecycle.on_startup,
                profile.lifecycle.before_run,
                profile.lifecycle.after_run,
                profile.lifecycle.on_shutdown,
            ):
                if hook is not None:
                    hook(context)


def _task(request: str) -> TaskJob:
    return TaskJob(
        id=1,
        chat_id=42,
        text=request,
        created_at="2026-01-01T00:00:00Z",
    )


class _Chat:
    name = "conformance-chat"
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
    name = "conformance-runtime"
    provider_kind = "runtime"
    config_section = "conformance"

    def respond(self, identity, message, **kwargs):
        return RuntimeResult(final_text="response")

    def act_in_session(self, identity, message, **kwargs):
        return RuntimeResult(final_text="action")

    def model_summary(self, root=None):
        return "conformance runtime"

    def model_options(self):
        return ()

    def reset_usage(self):
        return None

    def health(self, root=None):
        return None
