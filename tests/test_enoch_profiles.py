from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.app.core import EnochApplication
from enoch.commands import config_command
from enoch.config import read_section
from enoch.identity import load_identity
from enoch.profiles import (
    AgentProfile,
    CommandSpec,
    LifecycleHooks,
    PROFILE_API_VERSION,
    ProfilePresentation,
    ProfileError,
    WorkflowPolicy,
    load_profile,
    register_profile,
)
from enoch.app.models import WorkStatusMessage
from enoch.tasks.queue import TaskJob
from enoch.profiles import registry as profile_registry
from enoch.providers import ChatEvent, ProviderHealth
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import task_queue_status


class _Chat:
    name = "profile-chat"
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
    name = "profile-runtime"
    provider_kind = "runtime"
    config_section = "profile-runtime"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def respond(self, identity, message, **kwargs):
        self.messages.append(message)
        return "profile response"

    def act_in_session(self, identity, message, **kwargs):
        self.messages.append(message)
        return "profile task response"

    def model_summary(self, root=None):
        return "AI model: profile-runtime"

    def model_options(self):
        return ()

    def reset_usage(self):
        return None

    def health(self, root=None):
        return ProviderHealth("profile runtime", True, "profile doctor", "ready")


class _EntryPoint:
    name = "researcher"

    def load(self):
        return lambda _root=None: AgentProfile(name="researcher")


class _EntryPoints(list):
    def select(self, *, group):
        return self if group == "our_ark.profiles" else ()


class EnochProfileTests(unittest.TestCase):
    def test_profile_command_can_queue_work_without_core_changes(self) -> None:
        def research(context):
            job = context.enqueue_task(
                f"Research {context.argument}",
                context="Use primary sources.",
            )
            return f"Queued research task #{job.id}."

        profile = AgentProfile(
            name="researcher",
            workflow=WorkflowPolicy(
                timeout_seconds=180,
                max_attempts=1,
            ),
            commands=(
                CommandSpec(
                    "research",
                    "queue a research task",
                    research,
                    usage="/research <topic> - queue a research task",
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
                profile=profile,
            )

            app.handle_event(_event("/research stable agent profiles"))

            queued = task_queue_status(root).pending
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].text, "Research stable agent profiles")
            self.assertEqual(queued[0].context, "Use primary sources.")
            self.assertEqual(queued[0].context_source, "profile:researcher")
            self.assertEqual(queued[0].source, "task")
            self.assertEqual(queued[0].trigger, "/research")
            self.assertEqual(queued[0].timeout_seconds, 180)
            self.assertEqual(queued[0].max_attempts, 1)
            events = load_task_events(root, task_id=queued[0].id)
            self.assertEqual(events[0].source, "task")
            self.assertEqual(events[0].event_actor, "human")
            self.assertEqual(events[0].trigger, "/research")
            self.assertEqual(chat.sent[-1][1], "Queued research task #1.")

            app.handle_event(_event("/help", message_id="message-help"))
            self.assertIn("Profile (researcher):", chat.sent[-1][1])
            self.assertIn("/research - queue a research task", chat.sent[-1][1])

            app.handle_event(_event("/help research", message_id="message-2"))
            self.assertEqual(
                chat.sent[-1][1],
                "/research <topic> - queue a research task",
            )

    def test_profile_presentation_controls_bounded_labels(self) -> None:
        profile = AgentProfile(
            name="researcher",
            commands=(
                CommandSpec("research", "queue research", lambda _context: "ready"),
            ),
            presentation=ProfilePresentation(
                display_name="Researcher",
                help_heading="Research tools",
                task_label="Research task",
            ),
        )
        with TemporaryDirectory() as temp:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                profile=profile,
            )

            app.handle_event(_event("/help"))
            help_message = chat.sent[-1][1]
            app.handle_event(_event("/status", message_id="status"))
            status_message = chat.sent[-1][1]
            work_message = app._format_work_status(
                WorkStatusMessage(
                    chat_id="room-1",
                    message_id="work",
                    request="Investigate the evidence.",
                    started_at=0,
                    task_id=7,
                )
            )
            final_message = app._format_task_final(
                TaskJob(
                    id=7,
                    chat_id="room-1",
                    text="Investigate the evidence.",
                    created_at="2026-07-22T00:00:00+00:00",
                ),
                "completed",
                "Done.",
            )

        self.assertIn("Research tools:", help_message)
        self.assertIn("Agent profile: Researcher (researcher)", status_message)
        self.assertTrue(work_message.startswith("Research task #7\n"))
        self.assertTrue(final_message.startswith("Research task #7 final update\n"))

    def test_profile_can_require_queued_work(self) -> None:
        profile = AgentProfile(
            name="researcher",
            workflow=WorkflowPolicy(allow_direct_work=False),
        )
        with TemporaryDirectory() as temp:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                profile=profile,
            )

            app.handle_event(_event("/do investigate now"))
            queue_status = task_queue_status(Path(temp))

        self.assertIn("does not permit immediate /do work", chat.sent[-1][1])
        self.assertEqual(queue_status.pending_count, 0)

    def test_prompt_contributors_receive_purpose_and_extend_runtime_input(self) -> None:
        purposes: list[str] = []

        def context(contribution):
            purposes.append(contribution.purpose)
            return "Prefer peer-reviewed primary sources."

        runtime = _Runtime()
        profile = AgentProfile(name="researcher", prompt_contributors=(context,))
        with TemporaryDirectory() as temp:
            app = EnochApplication(
                load_identity(),
                Path(temp),
                _Chat(),
                runtime=runtime,
                profile=profile,
            )

            app.handle_event(_event("What should we investigate?"))

        self.assertEqual(purposes, ["conversation"])
        self.assertIn("Profile context:", runtime.messages[-1])
        self.assertIn("Prefer peer-reviewed primary sources.", runtime.messages[-1])

    def test_lifecycle_hooks_wrap_each_application_run(self) -> None:
        events: list[str] = []
        profile = AgentProfile(
            name="researcher",
            lifecycle=LifecycleHooks(
                on_initialize=lambda _context: events.append("initialize"),
                before_run=lambda _context: events.append("before"),
                after_run=lambda _context: events.append("after"),
            ),
        )
        with TemporaryDirectory() as temp:
            app = EnochApplication(
                load_identity(),
                Path(temp),
                _Chat(),
                runtime=_Runtime(),
                profile=profile,
            )
            with patch.object(app, "_maybe_start_task_worker"):
                app.run_once()

        self.assertEqual(events, ["initialize", "before", "after"])

    def test_profile_cannot_shadow_core_commands(self) -> None:
        profile = AgentProfile(
            name="researcher",
            commands=(CommandSpec("task", "shadow task", lambda _context: "no"),),
        )
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProfileError, "conflicts with core commands: /task"):
                EnochApplication(
                    load_identity(),
                    Path(temp),
                    _Chat(),
                    runtime=_Runtime(),
                    profile=profile,
                )

    def test_profile_registry_supports_static_and_entry_point_packages(self) -> None:
        with patch.dict(profile_registry._REGISTERED, {}, clear=True), patch.object(
            profile_registry,
            "_entry_points",
            return_value=_EntryPoints([_EntryPoint()]),
        ):
            register_profile("local", lambda _root=None: AgentProfile(name="local"))

            self.assertEqual(load_profile(name="local").name, "local")
            self.assertEqual(load_profile(name="researcher").name, "researcher")

            with TemporaryDirectory() as temp:
                root = Path(temp)
                config = root / ".enoch" / "config.yaml"
                config.parent.mkdir()
                config.write_text("agent:\n  profile: local\n", encoding="utf-8")
                self.assertEqual(load_profile(root).name, "local")

    def test_config_selects_profile_for_restart_and_reports_running_profile(self) -> None:
        with patch.dict(profile_registry._REGISTERED, {}, clear=True), patch.object(
            profile_registry,
            "_entry_points",
            return_value=(),
        ), TemporaryDirectory() as temp:
            root = Path(temp)
            register_profile(
                "researcher",
                lambda _root=None: AgentProfile(name="researcher"),
            )

            initial = config_command(
                "/config profiles",
                root,
                runtime=_Runtime(),
                active_profile_name="enoch",
            )
            changed = config_command(
                "/config profile researcher",
                root,
                runtime=_Runtime(),
                active_profile_name="enoch",
            )
            reset = config_command(
                "/config profile default",
                root,
                runtime=_Runtime(),
                active_profile_name="enoch",
            )
            reset_section = read_section("agent", root)

        self.assertIn("Running: enoch", initial)
        self.assertIn("Available: enoch, researcher", initial)
        self.assertIn("Restart Enoch to activate it", changed)
        self.assertIn("Selected for restart: researcher", changed)
        self.assertEqual(reset_section, {})
        self.assertIn("Selected for restart: enoch", reset)

    def test_status_reports_active_profile(self) -> None:
        profile = AgentProfile(name="researcher")
        with TemporaryDirectory() as temp:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                profile=profile,
            )
            app.handle_event(_event("/status"))

        self.assertIn("Agent profile: researcher", chat.sent[-1][1])

    def test_profile_rejects_unsupported_api_version(self) -> None:
        with self.assertRaisesRegex(ProfileError, "uses API version 2"):
            AgentProfile(name="future", api_version=PROFILE_API_VERSION + 1)

    def test_profile_rejects_invalid_workflow_and_presentation_values(self) -> None:
        with self.assertRaisesRegex(ProfileError, "timeout must be a positive integer"):
            WorkflowPolicy(timeout_seconds=0)
        with self.assertRaisesRegex(ProfileError, "max attempts must be between"):
            WorkflowPolicy(max_attempts=0)
        with self.assertRaisesRegex(ProfileError, "must be a single line"):
            ProfilePresentation(help_heading="Research\nTools")

    def test_profile_registry_rejects_invalid_factory_results(self) -> None:
        with patch.dict(profile_registry._REGISTERED, {}, clear=True), patch.object(
            profile_registry,
            "_entry_points",
            return_value=(),
        ):
            register_profile("broken", lambda _root=None: object())  # type: ignore[arg-type]
            with self.assertRaisesRegex(ProfileError, "did not return AgentProfile"):
                load_profile(name="broken")

            register_profile(
                "mismatch",
                lambda _root=None: AgentProfile(name="different"),
            )
            with self.assertRaisesRegex(ProfileError, "returned profile name"):
                load_profile(name="mismatch")

    def test_profile_failures_are_isolated_and_audited(self) -> None:
        def fail_command(_context):
            raise RuntimeError("command exploded")

        def fail_prompt(_context):
            raise RuntimeError("prompt exploded")

        def fail_hook(_context):
            raise RuntimeError("hook exploded")

        profile = AgentProfile(
            name="faulty",
            commands=(CommandSpec("fault", "exercise failure isolation", fail_command),),
            prompt_contributors=(fail_prompt,),
            lifecycle=LifecycleHooks(on_initialize=fail_hook),
        )
        runtime = _Runtime()
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._record_system_event"
        ) as record_event:
            chat = _Chat()
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=runtime,
                profile=profile,
            )
            app.handle_event(_event("hello"))
            app.handle_event(_event("/fault", message_id="message-fault"))

        self.assertEqual(runtime.messages[-1].count("Profile context:"), 0)
        self.assertEqual(chat.sent[-1][1], "Profile command /fault failed: command exploded")
        events = [call.args[0] for call in record_event.call_args_list]
        self.assertIn("profile_lifecycle_failed", events)
        self.assertIn("profile_prompt_failed", events)
        self.assertIn("profile_command_failed", events)


def _event(text: str, *, message_id: str = "message-1") -> ChatEvent:
    return ChatEvent(
        cursor=message_id,
        conversation_id="room-1",
        message_id=message_id,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
