from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.app.core import EnochApplication
from enoch.identity import load_identity
from enoch.profiles import (
    AgentProfile,
    CommandSpec,
    LifecycleHooks,
    ProfileError,
    load_profile,
    register_profile,
)
from enoch.profiles import registry as profile_registry
from enoch.providers import ChatEvent, ProviderHealth
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
            self.assertEqual(chat.sent[-1][1], "Queued research task #1.")

            app.handle_event(_event("/help research", message_id="message-2"))
            self.assertEqual(
                chat.sent[-1][1],
                "/research <topic> - queue a research task",
            )

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


def _event(text: str, *, message_id: str = "message-1") -> ChatEvent:
    return ChatEvent(
        cursor=message_id,
        conversation_id="room-1",
        message_id=message_id,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
