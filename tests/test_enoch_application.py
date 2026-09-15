from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.application import (
    APPLICATION_COMPOSITION_API_VERSION,
    ApplicationComposition,
    ApplicationCompositionError,
    ApplicationPresentation,
    ApplicationProviderSelection,
    run_application,
)
from enoch.extensions import AgentExtension, ExtensionCommandSpec, ExtensionLifecycleHooks
from enoch.evolution.core import EvolveCandidate, EvolveProposal, EvolveReport, EvolveState
from enoch.identity import load_identity, update_mission
from enoch.memory.prompt import memory_for_prompt
from enoch.profiles import AgentProfile, CommandSpec
from enoch.providers import AgentRuntimeError, ChatEvent, ProviderHealth
from enoch.workflows import LocalWorkflowEngine
from our_ark_provider_kit import (
    BranchlessRepositoryFixture,
    IndependentReviewFixture,
)


ROOT = Path(__file__).resolve().parents[1]


class ApplicationCompositionTests(unittest.TestCase):
    def test_startup_receives_commands_without_invoking_a_runtime(self) -> None:
        for provider in ("codex", "claude", "composition-runtime"):
            with self.subTest(provider=provider), TemporaryDirectory() as temp:
                chat = _Chat()
                chat.command_prefix = "."
                runtime = _Runtime()
                runtime.name = provider
                app = EnochApplication(load_identity(), Path(temp), chat, runtime=runtime)
                with patch.object(runtime, "respond", side_effect=AssertionError("unexpected inference")) as respond, \
                     patch.object(runtime, "act_in_session", side_effect=AssertionError("unexpected work")) as act, \
                     patch.object(chat, "receive", return_value=(_event("/help", "startup-help"),)) as receive:
                    app.notify_startup()
                    app.run_once()

                respond.assert_not_called()
                act.assert_not_called()
                receive.assert_called_once()
                self.assertEqual(len(chat.sent), 2)
                self.assertIn(".help", chat.sent[0][1])
                self.assertIn(".status", chat.sent[1][1])

    def test_context_is_attached_to_the_first_real_request_per_session(self) -> None:
        with TemporaryDirectory() as temp:
            chat = _Chat()
            chat.command_prefix = "."
            runtime = _Runtime()
            app = EnochApplication(load_identity(), Path(temp), chat, runtime=runtime)
            with patch.object(runtime, "respond", return_value="response") as respond, \
                 patch("enoch.app.core.memory_for_prompt", return_value="Private identity and memory") as memory:
                app.notify_startup()
                memory.assert_not_called()
                respond.assert_not_called()
                for session, message in (("chat:one", "First question"), ("chat:one", "Next question"), ("chat:two", "Other question")):
                    app._respond_read_only_turn("room-1", message, session_key=session)

            prompts = [call.args[1] for call in respond.call_args_list]
            for index in (0, 2):
                self.assertIn("Private identity and memory", prompts[index])
                self.assertIn("Do not resume previous tasks", prompts[index])
                self.assertIn("Active chat command reference:", prompts[index])
                self.assertIn(".evolve config mode", prompts[index])
                self.assertIn(".evolve config schedule off", prompts[index])
                self.assertIn("Current request:", prompts[index])
            self.assertIn("First question", prompts[0])
            self.assertNotIn("Enoch startup context:", prompts[1])
            self.assertIn("Next question", prompts[1])
            self.assertEqual(memory.call_count, 2)

    def test_failed_first_response_keeps_context_for_the_next_request(self) -> None:
        with TemporaryDirectory() as temp:
            runtime = _Runtime()
            app = EnochApplication(load_identity(), Path(temp), _Chat(), runtime=runtime)
            with patch.object(runtime, "respond", side_effect=[AgentRuntimeError("unavailable"), "response"]) as respond:
                self.assertEqual(app._respond_read_only_turn("room-1", "First question"), "unavailable")
                self.assertEqual(app._respond_read_only_turn("room-1", "Try again"), "response")

            for call in respond.call_args_list:
                self.assertIn("Enoch startup context:", call.args[1])

    def test_restart_refreshes_context_without_a_standalone_model_turn(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = _Runtime()
            with patch.object(runtime, "respond", return_value="response") as respond:
                for expected_calls in (0, 1):
                    app = EnochApplication(load_identity(), root, _Chat(), runtime=runtime)
                    app.notify_startup()
                    self.assertEqual(respond.call_count, expected_calls)
                    app._respond_read_only_turn("room-1", "Current question")

            self.assertEqual(respond.call_count, 2)
            for call in respond.call_args_list:
                self.assertIn("Enoch startup context:", call.args[1])
                self.assertIn("Current question", call.args[1])

    def test_domain_help_hides_core_but_all_restores_it_for_each_prefix(self) -> None:
        for prefix in ("/", ".", "!"):
            with self.subTest(prefix=prefix), TemporaryDirectory() as temp:
                chat = _Chat()
                chat.command_prefix = prefix
                app = EnochApplication(
                    load_identity(), Path(temp), chat, runtime=_Runtime(),
                    presentation=ApplicationPresentation(default_help_scope="domain"),
                    profile=AgentProfile(name="research", commands=(
                        CommandSpec("sources", "list research sources", lambda _: "sources"),
                    )),
                    extensions=(AgentExtension(name="papers", help_heading="Research", commands=(
                        ExtensionCommandSpec("paper", "research papers", lambda _: "papers"),
                    )),),
                )
                app.handle_event(_event("/help", "compact"))
                compact = chat.sent[-1][1]
                self.assertIn(f"{prefix}paper - research papers", compact)
                self.assertIn(f"{prefix}sources - list research sources", compact)
                self.assertIn(f"{prefix}help --all", compact)
                self.assertNotIn(f"{prefix}status -", compact)
                app.handle_event(_event("/help --all", "all"))
                expanded = chat.sent[-1][1]
                self.assertIn(f"{prefix}status -", expanded)
                self.assertIn(f"{prefix}paper -", expanded)
                self.assertIn(f"{prefix}sources -", expanded)
                self.assertEqual(app._help("status"), app._help("/status"))
                app.handle_event(_event("/sources", "run-profile"))
                self.assertEqual(chat.sent[-1][1], "sources")
                app.handle_event(_event("/paper", "run-extension"))
                self.assertEqual(chat.sent[-1][1], "papers")

    def test_default_help_remains_complete_and_empty_domain_has_escape_hatch(self) -> None:
        with TemporaryDirectory() as temp:
            app = EnochApplication(load_identity(), Path(temp), _Chat(), runtime=_Runtime())
            self.assertEqual(app._help(""), app._help("--all"))
            app.presentation = ApplicationPresentation(default_help_scope="domain")
            self.assertIn("no domain commands", app._help(""))
            self.assertIn("/help --all", app._help(""))
            self.assertNotIn("/status -", app._help(""))
            self.assertIn("/status", app._help("status"))
            self.assertIn("/help --all", app._help("unknown-command"))

    def test_help_scope_rejects_unknown_values(self) -> None:
        for value in ("hidden", None, True):
            with self.subTest(value=value), self.assertRaises(ApplicationCompositionError):
                ApplicationPresentation(default_help_scope=value)

    def test_composition_resolves_descendant_owned_startup_components(self) -> None:
        identity = load_identity()
        chat = _Chat()
        runtime = _Runtime()
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        manager = AgentExtension(name="manager")
        configured = AgentExtension(name="configured")
        profile = AgentProfile(name="coordinator")
        provider_calls = []
        workflow_calls = []

        def provider(kind, root, *, name=""):
            provider_calls.append((kind, Path(root), name))
            return {
                "chat": chat,
                "runtime": runtime,
                "vcs": repository,
                "forge": review,
            }[kind]

        def extensions(_root, *, names=None):
            return (configured,) if names is None else (manager,)

        def workflow_factory(root, epoch):
            workflow_calls.append((root, epoch))
            return LocalWorkflowEngine(root, epoch=epoch)

        composition = ApplicationComposition(
            name="noah",
            identity_loader=lambda _path: identity,
            identity_path_resolver=lambda root: root / "src/noah/body.yaml",
            presentation=ApplicationPresentation(
                display_name="Noah",
                ready_message="Noah is ready to coordinate.",
            ),
            profile_name="coordinator",
            required_extensions=("manager",),
            providers=ApplicationProviderSelection(
                chat="default-chat",
                runtime="codex",
                vcs="local",
                forge="local",
            ),
            workflow_factory=workflow_factory,
        )
        with TemporaryDirectory() as temp, patch(
            "enoch.application.load_provider",
            side_effect=provider,
        ), patch(
            "enoch.application.load_profile",
            return_value=profile,
        ) as load_selected_profile, patch(
            "enoch.application.load_extensions",
            side_effect=extensions,
        ):
            root = Path(temp)
            components = composition.resolve(
                root,
                chat_provider_name="telegram",
            )

        self.assertEqual(
            composition.api_version,
            APPLICATION_COMPOSITION_API_VERSION,
        )
        self.assertEqual(components.composition_name, "noah")
        self.assertIs(components.identity, identity)
        self.assertEqual(
            components.identity_path,
            root.resolve() / "src/noah/body.yaml",
        )
        self.assertEqual(
            tuple(extension.name for extension in components.extensions),
            ("manager", "configured"),
        )
        self.assertIs(components.profile, profile)
        self.assertIs(components.chat, chat)
        self.assertIs(components.runtime, runtime)
        self.assertIs(components.repository, repository)
        self.assertIs(components.review, review)
        self.assertEqual(
            tuple((kind, name) for kind, _root, name in provider_calls),
            (
                ("chat", "telegram"),
                ("runtime", "codex"),
                ("vcs", "local"),
                ("forge", "local"),
            ),
        )
        load_selected_profile.assert_called_once_with(
            root.resolve(),
            name="coordinator",
        )
        self.assertEqual(len(workflow_calls), 1)
        self.assertIs(components.workflow.epoch, components.daemon_epoch)

    def test_composition_rejects_identity_path_outside_instance(self) -> None:
        composition = ApplicationComposition(
            identity_path_resolver=lambda _root: Path("/tmp/outside/identity.yaml")
        )
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(
                ApplicationCompositionError,
                "inside the instance root",
            ):
                composition.resolve(Path(temp))

    def test_composition_reloads_mutable_identity_after_restart(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            identity_path = root / "src/noah/body.yaml"
            identity_path.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "src/enoch/body.yaml", identity_path)
            composition = ApplicationComposition(
                name="noah",
                identity_loader=load_identity,
                identity_path_resolver=lambda _root: identity_path,
            )
            providers = {
                "chat": _Chat(),
                "runtime": _Runtime(),
                "vcs": BranchlessRepositoryFixture(),
                "forge": IndependentReviewFixture(),
            }
            with patch(
                "enoch.application.load_provider",
                side_effect=lambda kind, _root, *, name="": providers[kind],
            ), patch(
                "enoch.application.load_profile",
                return_value=AgentProfile(name="default"),
            ), patch(
                "enoch.application.load_extensions",
                return_value=(),
            ):
                first = composition.resolve(root)
                update_mission(
                    "Coordinate the durable project.",
                    path=identity_path,
                )
                restarted = composition.resolve(root)

        self.assertNotEqual(
            first.identity.mission,
            "Coordinate the durable project.",
        )
        self.assertEqual(
            restarted.identity.mission,
            "Coordinate the durable project.",
        )

    def test_custom_identity_path_and_presentation_drive_application(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            identity_path = root / "src/noah/body.yaml"
            identity_path.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "src/enoch/body.yaml", identity_path)
            identity_path.write_text(
                identity_path.read_text(encoding="utf-8").replace(
                    "name: Enoch",
                    "name: Noah",
                    1,
                ),
                encoding="utf-8",
            )
            chat = _Chat()
            app = EnochApplication(
                load_identity(identity_path),
                root,
                chat,
                runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(),
                review=IndependentReviewFixture(),
                identity_path=identity_path,
                presentation=ApplicationPresentation(
                    display_name="Noah",
                    ready_message="Noah is ready to coordinate.",
                ),
            )

            app.handle_event(_event("/start", "start"))
            app.handle_event(
                _event("/mission Coordinate the whole project.", "mission")
            )
            reloaded = load_identity(identity_path)
            prompt_memory = memory_for_prompt(
                root,
                identity=app.identity,
                identity_path=identity_path,
            )

        self.assertEqual(chat.sent[0][1].splitlines()[0], "Noah is ready to coordinate.")
        self.assertEqual(
            chat.sent[1][1],
            "Noah mission updated.\nMission: Coordinate the whole project.",
        )
        self.assertEqual(reloaded.mission, "Coordinate the whole project.")
        self.assertEqual(app.identity.mission, "Coordinate the whole project.")
        self.assertIn("Name: Noah", prompt_memory)
        self.assertIn("Mission: Coordinate the whole project.", prompt_memory)

    def test_run_application_delegates_to_enoch_owned_runner(self) -> None:
        composition = ApplicationComposition()
        with patch("enoch.app.core.main") as main:
            run_application(composition, chat_provider_name="telegram")

        main.assert_called_once_with(
            chat_provider_name="telegram",
            composition=composition,
        )

    def test_composition_validates_version_and_bounded_presentation(self) -> None:
        with self.assertRaisesRegex(
            ApplicationCompositionError,
            "supports version",
        ):
            ApplicationComposition(
                api_version=APPLICATION_COMPOSITION_API_VERSION + 1
            )
        with self.assertRaisesRegex(
            ApplicationCompositionError,
            "one line",
        ):
            ApplicationPresentation(ready_message="line one\nline two")

    def test_authenticated_peer_event_is_only_offered_to_extension_hook(self) -> None:
        received = []

        def on_peer(context, event, alias):
            received.append((context.identity.name, event.text, alias))
            return ""

        extension = AgentExtension(
            name="peer-test",
            lifecycle=ExtensionLifecycleHooks(on_peer_event=on_peer),
        )
        chat = _PeerChat()
        with TemporaryDirectory() as temp:
            app = EnochApplication(
                load_identity(),
                Path(temp),
                chat,
                runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(),
                review=IndependentReviewFixture(),
                extensions=(extension,),
            )
            app.handle_event(
                ChatEvent(
                    cursor="peer-2",
                    conversation_id="peer-room",
                    message_id="peer-1",
                    text="/shutdown",
                )
            )

        self.assertEqual(received, [("Enoch", "/shutdown", "lily")])
        self.assertEqual(chat.sent, [])

    def test_descendant_delivery_preserves_literal_content(self) -> None:
        identity = load_identity()
        messages = (
            f"Noah descends from {identity.name}.",
            f'> Human: "Ask {identity.name} to review this."',
            f'```python\nagent = "{identity.name}"\n```',
            f"Source: /tmp/{identity.name}/README.md",
            f"https://example.test/{identity.name}/review?q={identity.name}",
        )
        for message in messages:
            with self.subTest(message=message), TemporaryDirectory() as temp:
                chat = _Chat()
                app = self._presented_application(Path(temp), chat, identity)
                app._safe_send_message("room-1", message)
                app._safe_edit_message("room-1", "message-1", message)

                self.assertEqual(chat.sent, [("room-1", message)])
                self.assertEqual(chat.edited, [("room-1", "message-1", message)])
                self.assertIs(app.identity, identity)

    def test_descendant_natural_reply_is_delivered_verbatim(self) -> None:
        identity = load_identity()
        reply = f"Noah descends from {identity.name}. See /tmp/{identity.name}/README.md."
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(Path(temp), chat, identity)
            with patch.object(app, "_natural", return_value=reply):
                app.handle_event(_event("Who is your ancestor?", "ancestor"))

        self.assertEqual(chat.sent, [("room-1", reply)])

    def test_descendant_progress_names_only_the_system_prefix(self) -> None:
        identity = load_identity()
        message = f"Review {identity.name}'s changes in /tmp/{identity.name}."
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(Path(temp), chat, identity)
            app._send_step_update("room-1", message)

        self.assertEqual(chat.sent, [("room-1", f"Hosted {identity.name} update: {message}")])

    def test_descendant_lifecycle_preserves_context_and_body_identity(self) -> None:
        identity = load_identity()
        reason = f"maintenance of /tmp/{identity.name}"
        summary = f"Last update: imported {identity.name}'s changes."
        warning = f"Previous diagnostic: {identity.name} was unavailable."
        chat = _Chat()
        with TemporaryDirectory() as temp, patch(
            "enoch.app.core._sync_session_activity"
        ) as sync, patch("enoch.channel.repository_sync_summary", return_value=summary):
            app = self._presented_application(
                Path(temp), chat, identity, previous_shutdown_warning=warning
            )
            app.notify_startup()
            app.notify_shutdown(reason)

        self.assertEqual(
            chat.sent[0][1].splitlines()[0],
            f"Hosted {identity.name} restarted and is listening on Composition Chat.",
        )
        self.assertIn(summary, chat.sent[0][1])
        self.assertIn(warning, chat.sent[0][1])
        self.assertEqual(chat.sent[1][1].splitlines()[0], f"Hosted {identity.name} is shutting down.")
        self.assertIn(f"Reason: {reason}.", chat.sent[1][1])
        self.assertIs(app.identity, identity)
        sync.assert_not_called()

    def test_display_name_defaults_to_loaded_identity(self) -> None:
        identity = replace(load_identity(), name="Descendant")
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(
                Path(temp), chat, identity, presentation=ApplicationPresentation()
            )
            app._send_step_update("room-1", "Working.")
            app.notify_shutdown("test")

        self.assertEqual(chat.sent[0][1], "Descendant update: Working.")
        self.assertEqual(chat.sent[1][1].splitlines()[0], "Descendant is shutting down.")

    def test_custom_ready_message_is_literal(self) -> None:
        identity = load_identity()
        message = f"Built on {identity.name}; ready to coordinate."
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(
                Path(temp), chat, identity,
                presentation=ApplicationPresentation(
                    display_name=f"Hosted {identity.name}", ready_message=message
                ),
            )
            app.handle_event(_event("/start", "start"))

        self.assertEqual(chat.sent[0][1].splitlines()[0], message)

    def test_extension_reply_is_delivered_verbatim(self) -> None:
        identity = load_identity()
        reply = f"Extension reports: {identity.name}'s review is ready."
        extension = AgentExtension(
            name="literal-output",
            commands=(ExtensionCommandSpec("literal", "show literal text", lambda _ctx: reply),),
        )
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(Path(temp), chat, identity, extensions=(extension,))
            app.handle_event(_event("/literal", "literal"))

        self.assertEqual(chat.sent, [("room-1", reply)])

    def test_task_error_names_only_the_system_prefix(self) -> None:
        identity = load_identity()
        error = f"Cannot open /tmp/{identity.name}/README.md"
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(Path(temp), chat, identity)
            with patch.object(
                app, "_resolve_task_context_snapshot", return_value=TaskContextSnapshot(error=error)
            ):
                app.handle_event(_event(f"/task Review {identity.name}", "task"))

        self.assertEqual(
            chat.sent,
            [("room-1", f"Hosted {identity.name} could not prepare conversation context for that task yet: {error}")],
        )

    def test_proposal_names_only_the_system_heading(self) -> None:
        identity = load_identity()
        title = f"Learn from {identity.name}"
        candidate = EvolveCandidate(
            id="learning-fixture", source="learning", title=title,
            rationale=f"Inspect {identity.name}'s skill.", proposed_change="Import the skill.",
            expected_benefit="Reuse.", risk="low", test_plan="Run tests.",
        )
        report = EvolveReport(EvolveState(), (candidate,), candidate, {"learning": 1})
        proposal = EvolveProposal(report, (candidate,), candidate)
        chat = _Chat()
        with TemporaryDirectory() as temp:
            app = self._presented_application(Path(temp), chat, identity)
            with patch.object(app, "_propose_evolve", return_value=proposal):
                app.handle_event(_event("/evolve propose", "propose"))

        self.assertEqual(chat.sent[0][1].splitlines()[0], f"Hosted {identity.name} proposes:")
        self.assertIn(title, chat.sent[0][1])
        self.assertIn(candidate.rationale, chat.sent[0][1])

    @staticmethod
    def _presented_application(root, chat, identity, *, presentation=None, **kwargs):
        return EnochApplication(
            identity,
            root,
            chat,
            runtime=_Runtime(),
            repository=BranchlessRepositoryFixture(),
            review=IndependentReviewFixture(),
            presentation=presentation or ApplicationPresentation(display_name=f"Hosted {identity.name}"),
            **kwargs,
        )


class _Chat:
    name = "composition-chat"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.sent = []
        self.edited = []

    @property
    def allowed_conversation_id(self):
        return "room-1"

    def receive(self, cursor=None):
        return ()

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "message-1"

    def edit_message(self, conversation_id, message_id, text):
        self.edited.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _PeerChat(_Chat):
    def peer_alias(self, event):
        return "lily" if event.conversation_id == "peer-room" else None


class _Runtime:
    name = "composition-runtime"
    provider_kind = "runtime"
    config_section = "composition-runtime"

    def respond(self, identity, message, **kwargs):
        return "response"

    def act_in_session(self, identity, message, **kwargs):
        return "task response"

    def model_summary(self, root=None):
        return "composition runtime"

    def model_options(self):
        return ()

    def reset_usage(self):
        return None

    def health(self, root=None):
        return ProviderHealth("composition runtime", True, "doctor", "ready")


def _event(text: str, message_id: str) -> ChatEvent:
    return ChatEvent(
        cursor=message_id,
        conversation_id="room-1",
        message_id=message_id,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
