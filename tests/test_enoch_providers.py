from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.commands import config_command
from enoch.config import read_section
from enoch.git_tools import run_git
from enoch.identity import load_identity
from enoch.immune import _git_worktree_check, _runtime_provider_check
from enoch.operations.update_tools import task_branch_base
from enoch.operations.updater import update_from_authoritative
from enoch.providers import (
    Attachment,
    ChatEvent,
    LocalPublishResult,
    PullRequestResult,
    ProviderError,
    ProviderHealth,
    RemotePublishResult,
    available_providers,
    load_provider,
    provider_name,
    register_provider,
)
from enoch.providers.forge import LocalForgeProvider
from enoch.providers import registry as provider_registry
from enoch.app.core import EnochApplication
from enoch.tasks.queue import enqueue_task, record_task_status_message, task_queue_status
from enoch.tasks.worktree import TaskWorktree


class _Result:
    returncode = 0
    stdout = "custom vcs"
    stderr = ""


class _Vcs:
    name = "test-vcs"
    provider_kind = "vcs"

    def __init__(self) -> None:
        self.calls = []

    def run(self, args, root=None):
        self.calls.append((args, root))
        return _Result()

    def current_branch(self, root=None):
        return "main"

    def is_clean(self, root=None):
        return True

    def changed_files(self, root=None):
        return []

    def diff_summary(self, root=None):
        return "No working tree changes."

    def stage(self, files, root=None):
        return None

    def commit(self, message, root=None):
        return "revision-1"

    def create_branch(self, branch, root=None, *, start_point=""):
        return None

    def switch_branch(self, branch, root=None):
        return None

    def delete_branch(self, branch, root=None, *, force=False):
        return None

    def branch_exists(self, branch, root=None):
        return True

    def task_base(self, root=None):
        return "revision-1"

    def authoritative_branch(self, root=None):
        return "main"

    def refresh_authoritative(self, root=None):
        return ""

    def authoritative_revision(self, root=None):
        return "revision-1"

    def current_revision(self, root=None):
        return "revision-1"

    def resolve_revision(self, revision, root=None):
        return revision

    def is_ancestor(self, revision, descendant, root=None):
        return True

    def update_to_authoritative(self, root=None):
        return "Already up to date."

    def restore_revision(self, revision, root=None):
        return None

    def workspace_paths(self, root=None):
        return ()

    def create_workspace(
        self,
        path,
        branch,
        root=None,
        *,
        start_point="",
        create_branch=False,
    ):
        return None

    def remove_workspace(self, path, root=None):
        return None


class _SemanticVcs(_Vcs):
    name = "semantic-vcs"

    def __init__(self) -> None:
        super().__init__()
        self.staged = ()
        self.commits = []
        self.refreshed = False
        self.updated = False
        self.restored = ""

    def current_branch(self, root=None):
        return "change/portable"

    def is_clean(self, root=None):
        return True

    def task_base(self, root=None):
        return "stable-revision"

    def authoritative_branch(self, root=None):
        return "trunk"

    def refresh_authoritative(self, root=None):
        self.refreshed = True
        return "refreshed trunk"

    def authoritative_revision(self, root=None):
        return "revision-2"

    def current_revision(self, root=None):
        return "revision-2" if self.updated else "revision-1"

    def resolve_revision(self, revision, root=None):
        return {"trunk": "revision-1"}.get(revision, revision)

    def is_ancestor(self, revision, descendant, root=None):
        return (revision, descendant) in {
            ("revision-1", "revision-2"),
            ("revision-2", "revision-2"),
        }

    def update_to_authoritative(self, root=None):
        self.updated = True
        return "updated to trunk"

    def restore_revision(self, revision, root=None):
        self.restored = revision

    def changed_files(self, root=None):
        return ["README.md"]

    def diff_summary(self, root=None):
        return "README.md changed"

    def stage(self, files, root=None):
        self.staged = tuple(files)

    def commit(self, message, root=None):
        self.commits.append(message)
        return "revision-1"


class _Runtime:
    name = "test-runtime"
    provider_kind = "runtime"
    config_section = "test-runtime"

    def __init__(self) -> None:
        self.messages = []
        self.reset_count = 0

    def respond(self, identity, message, **kwargs):
        self.messages.append((identity, message, kwargs))
        return "Hello from a plugin runtime."

    def act_in_session(self, identity, message, **kwargs):
        return "Plugin work completed."

    def model_summary(self, root=None):
        return "AI model: plugin-model"

    def model_options(self):
        return ()

    def reset_usage(self):
        self.reset_count += 1

    def health(self, root=None):
        return ProviderHealth(
            name="test runtime",
            passed=True,
            command="test-runtime doctor",
            summary="ready",
        )


class _ConfigurableRuntime(_Runtime):
    name = "configurable-runtime"
    config_section = "configurable-runtime"

    def configure(self, args, root, *, prefix="/"):
        return f"Configured {self.name}: {' '.join(args) or 'status'} ({prefix})"

    def config_summary(self, root):
        return "Endpoint: local-test"


class _Chat:
    name = "test-chat"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.sent = []
        self.acks = []
        self.downloaded = []
        self.events = []
        self.cursors = []

    @property
    def allowed_conversation_id(self):
        return "room-1"

    def receive(self, cursor=None):
        self.cursors.append(cursor)
        events, self.events = self.events, []
        return events

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "message-2"

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        self.acks.append((conversation_id, message_id))

    def download_attachment(self, attachment, destination, *, max_bytes):
        self.downloaded.append((attachment.file_id, max_bytes))
        destination.write_bytes(b"\xff\xd8\xffplugin-image")


class _Service:
    name = "test-service"
    provider_kind = "service"

    def install(self, root=None): return "installed"
    def uninstall(self, root=None): return "uninstalled"
    def start(self, root=None): return "started"
    def stop(self, root=None, *, allow_missing=False): return "stopped"
    def restart(self, root=None): return "restarted"
    def status(self, root=None): return "running"
    def logs(self, root=None, *, lines=80): return "logs"
    def doctor(self, root=None): return "passed"
    def manifest(self, root=None): return "manifest"
    def schedule_restart(self, root=None): return None
    def schedule_stop(self, root=None): return None


class _EntryPoint:
    name = "chat.entry-chat"

    def load(self):
        return lambda _root=None: _Chat()


class _EntryPoints(list):
    def select(self, *, group):
        return self if group == "enoch.providers" else ()


class EnochProviderTests(unittest.TestCase):
    def test_defaults_preserve_existing_stack(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            self.assertEqual(provider_name("chat", root), "telegram")
            self.assertEqual(provider_name("runtime", root), "codex")
            self.assertEqual(provider_name("vcs", root), "git")
            self.assertEqual(provider_name("forge", root), "github")
            self.assertIn(provider_name("service", root), {"launchd", "systemd"})

    def test_registered_provider_can_be_selected_from_instance_config(self) -> None:
        provider = _Vcs()
        register_provider("vcs", "test-vcs", lambda _root=None: provider, replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  vcs: test-vcs\n", encoding="utf-8")

            result = run_git(["status", "--short"], root)

        self.assertEqual(result.stdout, "custom vcs")
        self.assertEqual(provider.calls, [(["status", "--short"], root)])

    def test_invalid_provider_reports_missing_contract_members_at_load_time(self) -> None:
        invalid = type("InvalidChat", (), {"name": "broken", "provider_kind": "chat"})()
        register_provider("chat", "broken", lambda _root=None: invalid, replace=True)

        with self.assertRaisesRegex(ProviderError, "Missing: allowed_conversation_id"):
            load_provider("chat", name="broken")

    def test_core_starts_with_only_custom_chat_and_vcs_integrations(self) -> None:
        with TemporaryDirectory() as temp, patch.dict(
            provider_registry._REGISTERED,
            {},
            clear=True,
        ), patch.dict(
            provider_registry._DEFAULTS,
            {},
            clear=True,
        ), patch.object(
            provider_registry,
            "_LOADED_PLUGIN_MODULES",
            set(),
        ), patch.object(
            provider_registry,
            "load_runtime_dependencies",
            return_value=(),
        ), patch.object(
            provider_registry,
            "_entry_points",
            return_value=(),
        ):
            root = Path(temp)
            register_provider("chat", "custom-chat", lambda _root=None: _Chat())
            register_provider("vcs", "custom-vcs", lambda _root=None: _Vcs())
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "providers:\n  chat: custom-chat\n  vcs: custom-vcs\n",
                encoding="utf-8",
            )

            chat = load_provider("chat", root)
            runtime = load_provider("runtime", root)
            forge = load_provider("forge", root)
            app = EnochApplication(
                identity=load_identity(),
                root=root,
                client=chat,
                runtime=runtime,
                forge=forge,
            )

            self.assertEqual(app.client.name, "test-chat")
            self.assertEqual(runtime.name, "codex")
            self.assertIsInstance(forge, LocalForgeProvider)
            self.assertEqual(provider_name("vcs", root), "custom-vcs")
            status = config_command("/config providers", root)
            self.assertIn("forge: local", status)
            self.assertIn("service: not configured", status)

    def test_local_forge_publishes_through_semantic_vcs_contract(self) -> None:
        vcs = _SemanticVcs()
        doctor = MagicMock(passed=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            register_provider("vcs", "semantic-vcs", lambda _root=None: vcs, replace=True)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  vcs: semantic-vcs\n  forge: local\n", encoding="utf-8")

            with patch("enoch.providers.forge.run_immune_system", return_value=doctor):
                committed = LocalForgeProvider().prepare_local_publish(
                    "Portable change",
                    root=root,
                    allowed_files=("README.md",),
                )
            remote = LocalForgeProvider().push_current_branch(root=root)
            base = task_branch_base(root)
            health = _git_worktree_check(root, timeout=1)

        self.assertEqual(vcs.calls, [])
        self.assertEqual(vcs.staged, ("README.md",))
        self.assertEqual(vcs.commits, ["Portable change"])
        self.assertEqual(committed.commit_sha, "revision-1")
        self.assertFalse(remote.pushed)
        self.assertEqual(base, "stable-revision")
        self.assertTrue(health.passed)

    def test_update_uses_advanced_vcs_contract_without_raw_commands(self) -> None:
        vcs = _SemanticVcs()
        doctor = MagicMock(passed=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            register_provider("vcs", "semantic-vcs", lambda _root=None: vcs, replace=True)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  vcs: semantic-vcs\n", encoding="utf-8")

            with patch("enoch.operations.updater.run_update_doctor", return_value=doctor):
                result = update_from_authoritative(root)

        self.assertTrue(result.restart_required)
        self.assertTrue(vcs.refreshed)
        self.assertTrue(vcs.updated)
        self.assertEqual(vcs.calls, [])
        self.assertIn("updated to trunk", result.direct_action_result)

    def test_local_forge_handoff_preserves_unpushed_task_branch(self) -> None:
        doctor = MagicMock(passed=True)
        committed = LocalPublishResult(
            branch="change/portable",
            commit_message="Portable change",
            changed_files=["README.md"],
            diff="README.md changed",
            doctor=doctor,
            commit_sha="revision-1",
        )
        remote = RemotePublishResult(
            branch="change/portable",
            remote="local",
            pushed=False,
            ahead_count=0,
            compare_url=None,
        )
        review = PullRequestResult(
            branch="change/portable",
            title="Portable change",
            body="",
            created=False,
            url=None,
            fallback_url=None,
            note="No forge provider is configured.",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            app = EnochApplication(load_identity(), root, _Chat(), runtime=_Runtime())
            app.forge = MagicMock()
            app.forge.create_pull_request.return_value = review
            workspace = TaskWorktree(1, root / "task-1", "change/portable", True)
            with patch("enoch.app.core.feature_title", return_value="Portable change"), patch(
                "enoch.app.core.prepare_local_publish",
                return_value=committed,
            ), patch(
                "enoch.app.core.push_current_branch",
                return_value=remote,
            ), patch(
                "enoch.app.core.remove_task_worktree",
                return_value="Removed workspace and kept branch.",
            ) as remove:
                result = app._publish_feature_pr(
                    "room-1",
                    "Portable change",
                    ("README.md",),
                    work_root=workspace.path,
                    task_worktree=workspace,
                )

        remove.assert_called_once_with(
            root,
            workspace,
            delete_local_branch=False,
            force_delete_branch=False,
        )
        self.assertIn("kept this branch locally", result)

    def test_config_lists_and_selects_installed_providers(self) -> None:
        register_provider("runtime", "test-runtime", lambda _root=None: _Runtime(), replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)

            status = config_command("/config providers", root)
            changed = config_command("/config provider runtime test-runtime", root)

            self.assertIn("runtime: codex", status)
            self.assertIn("test-runtime", status)
            self.assertIn("runtime provider set to test-runtime", changed)
            self.assertEqual(provider_name("runtime", root), "test-runtime")

    def test_runtime_provider_owns_provider_specific_config(self) -> None:
        runtime = _ConfigurableRuntime()
        register_provider(
            "runtime",
            runtime.name,
            lambda _root=None: runtime,
            replace=True,
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)

            configured = config_command(
                "/config runtime configurable-runtime endpoint local-test",
                root,
            )
            status = config_command("/config", root, runtime=runtime)

        self.assertEqual(
            configured,
            "Configured configurable-runtime: endpoint local-test (/)",
        )
        self.assertIn("Endpoint: local-test", status)
        self.assertNotIn("Codex runtime executable", status)

    @patch.dict("os.environ", {}, clear=True)
    def test_config_sets_shows_and_resets_codex_executable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "Codex Runtime" / "codex"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)

            changed = config_command(
                f'/config runtime codex executable "{executable}"',
                root,
            )
            shown = config_command("/config runtime codex executable", root)
            configured = read_section("codex", root)
            reset = config_command("/config runtime codex executable auto", root)

            self.assertEqual(configured["executable"], str(executable))
            self.assertIn(f"Executable: {executable}", changed)
            self.assertIn("Source: Enoch config codex.executable", shown)
            self.assertIn("reset to automatic discovery", reset)
            self.assertNotIn("executable", read_section("codex", root))

    @patch.dict("os.environ", {}, clear=True)
    def test_doctor_reports_configured_codex_executable_source(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "codex"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                f'codex:\n  executable: "{executable}"\n',
                encoding="utf-8",
            )

            check = _runtime_provider_check(root)

        self.assertTrue(check.passed)
        self.assertIn(str(executable), check.summary)
        self.assertIn("source: Enoch config codex.executable", check.summary)

    def test_doctor_uses_selected_runtime_health(self) -> None:
        register_provider("runtime", "test-runtime", lambda _root=None: _Runtime(), replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  runtime: test-runtime\n", encoding="utf-8")

            check = _runtime_provider_check(root)

        self.assertTrue(check.passed)
        self.assertEqual(check.name, "test runtime")
        self.assertEqual(check.command, "test-runtime doctor")

    def test_normalized_chat_event_uses_runtime_without_telegram_payloads(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            runtime = _Runtime()
            bot = EnochApplication(load_identity(), root, chat, runtime=runtime)
            event = ChatEvent(
                cursor=2,
                conversation_id="room-1",
                message_id="message-1",
                text="hello",
            )

            with (
                patch("enoch.app.core.log_conversation_turn"),
                patch("enoch.app.core.ensure_long_term_memory"),
                patch("enoch.app.core.save_channel_cursor"),
                patch.object(bot, "_queue_session_sync"),
            ):
                bot.handle_event(event)

        self.assertEqual(chat.acks, [("room-1", "message-1")])
        self.assertEqual(chat.sent, [("room-1", "Hello from a plugin runtime.")])
        self.assertEqual(runtime.reset_count, 1)
        self.assertEqual(runtime.messages[0][2]["session_key"], "test-chat:room-1")

    def test_custom_chat_provider_persists_opaque_cursor(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            runtime = _Runtime()
            chat.events = [
                ChatEvent(
                    cursor="next-page-token",
                    conversation_id="room-1",
                    message_id="message-1",
                    text="/status",
                )
            ]
            app = EnochApplication(load_identity(), root, chat, runtime=runtime)

            with (
                patch("enoch.app.core.log_conversation_turn"),
                patch("enoch.app.core.ensure_long_term_memory"),
            ):
                app.run_once()
                restarted = EnochApplication(load_identity(), root, chat, runtime=runtime)
                restarted.run_once()

            state = (root / ".enoch" / "channels" / "test-chat" / "cursor.json").read_text(
                encoding="utf-8"
            )

        self.assertEqual(chat.cursors, [None, "next-page-token"])
        self.assertIn('"cursor": "next-page-token"', state)
        self.assertIn("Test Chat conversation id: room-1", chat.sent[0][1])
        self.assertNotIn("Telegram", chat.sent[0][1])

    def test_custom_chat_provider_supplies_image_attachment(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            runtime = _Runtime()
            app = EnochApplication(load_identity(), root, chat, runtime=runtime)
            event = ChatEvent(
                cursor="image-2",
                conversation_id="room-1",
                message_id="message-1",
                text="What is this?",
                attachments=(
                    Attachment(
                        kind="image",
                        file_id="plugin-file-1",
                        mime_type="image/jpeg",
                        size=128,
                    ),
                ),
            )

            with (
                patch("enoch.app.core.log_conversation_turn"),
                patch("enoch.app.core.ensure_long_term_memory"),
            ):
                app.handle_event(event)

            image_path = runtime.messages[0][2]["image_paths"][0]

        self.assertEqual(chat.downloaded[0][0], "plugin-file-1")
        self.assertFalse(image_path.exists())
        self.assertIn("Test Chat conversation", runtime.messages[0][1])
        self.assertNotIn("Telegram image boundary", runtime.messages[0][1])

    def test_custom_service_provider_can_be_selected(self) -> None:
        service = _Service()
        register_provider("service", "test-service", lambda _root=None: service, replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  service: test-service\n", encoding="utf-8")

            selected = load_provider("service", root)

        self.assertIs(selected, service)

    def test_string_chat_and_message_ids_survive_task_persistence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = enqueue_task("room-1", "plugin task", root)
            record_task_status_message(job.id, "message-9", root)

            pending = task_queue_status(root).pending[0]

        self.assertEqual(pending.chat_id, "room-1")
        self.assertEqual(pending.status_message_id, "message-9")

    def test_builtin_providers_are_discoverable(self) -> None:
        self.assertIn("telegram", available_providers("chat"))
        self.assertIn("codex", available_providers("runtime"))
        self.assertEqual(load_provider("vcs", name="git").name, "git")
        self.assertTrue(available_providers("service"))

    def test_installed_entry_point_loads_without_core_changes(self) -> None:
        with patch(
            "enoch.providers.registry.metadata.entry_points",
            return_value=_EntryPoints([_EntryPoint()]),
        ):
            provider = load_provider("chat", name="entry-chat")

        self.assertEqual(provider.name, "test-chat")
        self.assertEqual(provider.provider_kind, "chat")


if __name__ == "__main__":
    unittest.main()
