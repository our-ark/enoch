from pathlib import Path
import unittest

from enoch.app.epoch import DaemonEpoch
from enoch.conformance import (
    AgentExtensionConformanceMixin,
    AgentRuntimeConformanceMixin,
    DurableNotificationConformanceMixin,
    ExtensionCommandCase,
    ProfileCommandCase,
    ProfileConformanceMixin,
    WorkflowEngineConformanceMixin,
)
from enoch.extensions import (
    AgentExtension,
    ExtensionCommandSpec,
    ExtensionLifecycleHooks,
)
from enoch.profiles import (
    AgentProfile,
    CommandSpec,
    LifecycleHooks,
    WorkflowPolicy,
)
from enoch.providers import (
    NotificationCapabilities,
    NotificationDeliveryError,
    NotificationReceipt,
    ProviderHealth,
    RuntimeResult,
)
from enoch.providers.runtime import FunctionAgentRuntime
from enoch.workflows import LocalWorkflowEngine


class FunctionRuntimeConformanceTests(
    AgentRuntimeConformanceMixin,
    unittest.TestCase,
):
    def create_runtime(self, root: Path) -> FunctionAgentRuntime:
        del root

        def respond(_identity, message, **_kwargs):
            return RuntimeResult(final_text=f"response: {message}")

        def act(_identity, message, **_kwargs):
            return RuntimeResult(final_text=f"action: {message}")

        return FunctionAgentRuntime(
            respond_fn=respond,
            act_in_session_fn=act,
            model_summary_fn=lambda _root: "conformance runtime",
            model_options_fn=lambda: ("conformance",),
            reset_usage_fn=lambda: None,
            health_fn=lambda _root: ProviderHealth(
                name="conformance runtime",
                passed=True,
                command="conformance",
                summary="ready",
            ),
            name="conformance",
            config_section="conformance",
        )


class LocalWorkflowConformanceTests(
    WorkflowEngineConformanceMixin,
    unittest.TestCase,
):
    def create_workflow(
        self,
        root: Path,
        *,
        epoch: DaemonEpoch,
    ) -> LocalWorkflowEngine:
        return LocalWorkflowEngine(root, epoch=epoch)


class AgentProfileConformanceTests(
    ProfileConformanceMixin,
    unittest.TestCase,
):
    def setUp(self) -> None:
        self.lifecycle_events: list[str] = []

    def create_profile(self) -> AgentProfile:
        def research(command):
            command.enqueue_task(
                f"Research {command.argument}",
                context="Use cited primary sources.",
                required_capabilities=("runtime.execute",),
            )
            return "Research queued."

        return AgentProfile(
            name="researcher",
            commands=(
                CommandSpec(
                    name="research",
                    summary="Queue governed research.",
                    handler=research,
                ),
            ),
            prompt_contributors=(
                lambda context: f"Profile purpose: {context.purpose}.",
            ),
            workflow=WorkflowPolicy(timeout_seconds=180, max_attempts=1),
            lifecycle=LifecycleHooks(
                on_initialize=lambda _context: self.lifecycle_events.append(
                    "initialize"
                ),
                on_shutdown=lambda _context: self.lifecycle_events.append("shutdown"),
            ),
        )

    def command_case(self) -> ProfileCommandCase:
        return ProfileCommandCase(
            command="research",
            argument="durable agents",
            expected_request="Research durable agents",
            expected_context="Use cited primary sources.",
            expected_capabilities=("runtime.execute",),
        )

    def test_lifecycle_hooks_were_exercised_by_conformance_suite(self) -> None:
        self.test_conformance_profile_lifecycle_accepts_isolated_context()

        self.assertEqual(self.lifecycle_events, ["initialize", "shutdown"])


class AgentExtensionConformanceTests(
    AgentExtensionConformanceMixin,
    unittest.TestCase,
):
    def setUp(self) -> None:
        self.lifecycle_events: list[str] = []

    def create_extension(self) -> AgentExtension:
        def research(command):
            command.enqueue_task(
                f"Research {command.argument}",
                context="Use cited primary sources.",
                required_capabilities=("runtime.execute",),
                idempotency_key=f"research:{command.argument}",
            )
            return "Research queued."

        return AgentExtension(
            name="research",
            commands=(
                ExtensionCommandSpec(
                    name="research",
                    summary="Queue governed research.",
                    handler=research,
                ),
            ),
            lifecycle=ExtensionLifecycleHooks(
                on_initialize=lambda _context: self.lifecycle_events.append(
                    "initialize"
                ),
                on_shutdown=lambda _context: self.lifecycle_events.append("shutdown"),
            ),
        )

    def command_case(self) -> ExtensionCommandCase:
        return ExtensionCommandCase(
            command="research",
            argument="durable-agents",
            expected_request="Research durable-agents",
            expected_context="Use cited primary sources.",
            expected_capabilities=("runtime.execute",),
            idempotency_key="research:durable-agents",
        )

    def test_lifecycle_hooks_were_exercised_by_extension_conformance(self) -> None:
        self.test_conformance_extension_lifecycle_accepts_isolated_context()

        self.assertEqual(self.lifecycle_events, ["initialize", "shutdown"])


class DurableNotificationConformanceTests(
    DurableNotificationConformanceMixin,
    unittest.TestCase,
):
    def create_notification_provider(self, root: Path):
        del root
        return _DurableChat()

    def fail_next_notification(self, provider) -> None:
        provider.failures_remaining += 1

    def notification_attempts(self, provider, idempotency_key: str) -> int:
        return provider.attempts.get(idempotency_key, 0)


class _DurableChat:
    name = "conformance-chat"
    provider_kind = "chat"
    allowed_conversation_id = 42
    notification_capabilities = NotificationCapabilities(
        idempotent_delivery=True,
        reconciliation=True,
    )

    def __init__(self) -> None:
        self.attempts: dict[str, int] = {}
        self.receipts: dict[str, NotificationReceipt] = {}
        self.failures_remaining = 0

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        return 1

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        return None

    def deliver_notification(self, intent):
        key = intent.idempotency_key
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise NotificationDeliveryError(
                "injected partial delivery failure",
                retryable=True,
            )
        receipt = NotificationReceipt(
            idempotency_key=key,
            status="delivered",
            message_id=len(self.receipts) + 1,
        )
        self.receipts[key] = receipt
        return receipt

    def reconcile_notification(self, intent):
        return self.receipts.get(
            intent.idempotency_key,
            NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="not_found",
            ),
        )


if __name__ == "__main__":
    unittest.main()
