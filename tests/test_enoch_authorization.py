from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from enoch.app.core import EnochApplication
from enoch.identity import load_identity
from enoch.profiles import AgentProfile, CapabilityPolicy, CommandSpec
from enoch.providers import (
    AuthorizationDecision,
    ProviderCapabilities,
    RuntimeResult,
)
from enoch.providers.authorization import (
    DEFAULT_TASK_REQUIREMENTS,
    CapabilityAuthorizationError,
    CapabilityAuthorizer,
)


class CapabilityAuthorizationTests(unittest.TestCase):
    def test_read_only_response_does_not_require_repository_work(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _LimitedRuntime("runtime.respond")
            app = EnochApplication(
                load_identity(),
                Path(directory),
                _Chat(),
                runtime=runtime,
            )

            reply = app._respond_read_only_turn(42, "hello")

        self.assertEqual(reply, "response")
        self.assertEqual(runtime.respond_calls, 1)
        self.assertEqual(runtime.action_calls, 0)

    def test_task_missing_runtime_execute_fails_before_provider_call(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _LimitedRuntime("runtime.respond")
            app = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=runtime,
            )
            queued = app.workflow.enqueue(
                42,
                "change the repository",
                required_capabilities=DEFAULT_TASK_REQUIREMENTS.capabilities,
            )
            job = app.workflow.start_next()
            assert job is not None

            app._run_task_job(job)
            completed = app.workflow.inspect().history[-1]

        self.assertEqual(completed.id, queued.id)
        self.assertEqual(completed.status, "failed")
        self.assertEqual(completed.failure_code, "authorization_denied")
        self.assertIn("runtime.execute", completed.result)
        self.assertEqual(runtime.action_calls, 0)

    def test_profile_policy_can_tighten_task_authority(self) -> None:
        profile = AgentProfile(
            name="local-reviewer",
            authorization=CapabilityPolicy(
                denied_capabilities=("forge.review",),
                reason="This profile keeps repository work local.",
            ),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _LimitedRuntime("runtime.respond", "runtime.execute")
            app = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=runtime,
                profile=profile,
            )
            queued = app.workflow.enqueue(
                42,
                "change the repository",
                required_capabilities=DEFAULT_TASK_REQUIREMENTS.capabilities,
            )
            job = app.workflow.start_next()
            assert job is not None

            app._run_task_job(job)
            completed = app.workflow.inspect().history[-1]

        self.assertEqual(completed.id, queued.id)
        self.assertEqual(completed.failure_code, "authorization_denied")
        self.assertIn("keeps repository work local", completed.result)
        self.assertEqual(runtime.action_calls, 0)

    def test_profile_command_requirements_are_checked_before_handler(self) -> None:
        called = []

        def merge(context):
            called.append(context.argument)
            return "merged"

        profile = AgentProfile(
            name="reviewer",
            commands=(
                CommandSpec(
                    name="merge-review",
                    summary="merge reviewed work",
                    handler=merge,
                    required_capabilities=("forge.merge",),
                ),
            ),
        )
        with TemporaryDirectory() as directory:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(directory),
                chat,
                runtime=_LimitedRuntime("runtime.respond", "runtime.execute"),
                forge=_LimitedForge("forge.read", "forge.publish"),
                profile=profile,
            )

            app.handle_event(_event("/merge-review 12"))

        self.assertEqual(called, [])
        self.assertIn("forge.merge", chat.sent[-1][1])

    def test_allowing_policy_cannot_restore_missing_provider_grant(self) -> None:
        provider = _LimitedRuntime("runtime.respond")
        authorizer = CapabilityAuthorizer(
            lambda _kind: provider,
            policy=_AllowPolicy(),
        )

        with self.assertRaises(CapabilityAuthorizationError) as raised:
            authorizer.require("runtime.execute", ("runtime.execute",))

        self.assertEqual(
            raised.exception.decision.denied_capabilities,
            ("runtime.execute",),
        )

    def test_application_accepts_injected_authorization_policy(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _LimitedRuntime("runtime.respond", "runtime.execute")
            app = EnochApplication(
                load_identity(),
                Path(directory),
                _Chat(),
                runtime=runtime,
                authorization_policy=_DenyPolicy("runtime.respond"),
            )

            reply = app._respond_read_only_turn(42, "hello")

        self.assertIn("runtime.respond", reply)
        self.assertEqual(runtime.respond_calls, 0)

    def test_task_requirements_are_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=_LimitedRuntime("runtime.respond", "runtime.execute"),
            )
            queued = app.workflow.enqueue(
                42,
                "publish work",
                required_capabilities=(
                    "runtime.execute",
                    "forge.publish",
                ),
            )
            restored = app.workflow.find(queued.id)

        self.assertEqual(
            restored.required_capabilities,
            ("runtime.execute", "forge.publish"),
        )


class _AllowPolicy:
    def authorize(self, request):
        return AuthorizationDecision(allowed=True)


class _DenyPolicy:
    def __init__(self, *capabilities: str) -> None:
        self.capabilities = frozenset(capabilities)

    def authorize(self, request):
        denied = tuple(
            capability
            for capability in request.requirements.capabilities
            if capability in self.capabilities
        )
        return (
            AuthorizationDecision(
                allowed=False,
                reason="Denied by the embedding application.",
                denied_capabilities=denied,
            )
            if denied
            else AuthorizationDecision(allowed=True)
        )


class _LimitedRuntime:
    name = "limited-runtime"
    provider_kind = "runtime"
    config_section = "limited"

    def __init__(self, *capabilities: str) -> None:
        self.capabilities = ProviderCapabilities(
            provider_kind="runtime",
            capabilities=frozenset(capabilities),
        )
        self.respond_calls = 0
        self.action_calls = 0

    def respond(self, identity, prompt, **kwargs):
        self.respond_calls += 1
        return RuntimeResult(final_text="response")

    def act_in_session(self, identity, prompt, **kwargs):
        self.action_calls += 1
        return RuntimeResult(final_text="action")

    def model_summary(self, root=None):
        return "limited runtime"

    def model_options(self):
        return ("limited",)

    def reset_usage(self):
        return None


class _LimitedForge:
    name = "limited-forge"
    provider_kind = "forge"

    def __init__(self, *capabilities: str) -> None:
        self.capabilities = ProviderCapabilities(
            provider_kind="forge",
            capabilities=frozenset(capabilities),
        )


class _Chat:
    name = "test"
    provider_kind = "chat"
    allowed_conversation_id = 42

    def __init__(self) -> None:
        self.sent = []

    def receive(self, cursor=None):
        return ()

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        return None


def _event(text: str):
    from enoch.providers import ChatEvent

    return ChatEvent(
        cursor=1,
        conversation_id=42,
        message_id=1,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
