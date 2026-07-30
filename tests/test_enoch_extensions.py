from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.app.core import EnochApplication
from enoch.extensions import (
    AGENT_EXTENSION_API_VERSION,
    ExtensionCommandSpec,
    AgentExtension,
    AgentExtensionError,
    ExtensionLifecycleHooks,
    load_extensions,
    register_extension,
)
from enoch.extensions import registry as extension_registry
from enoch.identity import load_identity
from enoch.profiles import AgentProfile, CommandSpec
from enoch.providers import ChatEvent, ProviderHealth
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import task_queue_status


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
            "Extension command /fault failed: command exploded",
        )
        events = [call.args[0] for call in record_event.call_args_list]
        self.assertIn("agent_extension_lifecycle_failed", events)
        self.assertIn("agent_extension_command_failed", events)


def _event(text: str, *, message_id: str = "message-1") -> ChatEvent:
    return ChatEvent(
        cursor=message_id,
        conversation_id="room-1",
        message_id=message_id,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
