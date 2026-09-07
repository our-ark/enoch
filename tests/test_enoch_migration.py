from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile


from enoch.app.epoch import begin_daemon_epoch
from enoch.agent_identity import install_agent_identity
from enoch.migration import (
    AgentMigrationError,
    BUNDLE_MANIFEST,
    activate_imported_migration,
    cancel_exported_migration,
    export_migration_bundle,
    import_migration_bundle,
    inspect_migration_bundle,
    migration_status,
    source_migration_marker_path,
    target_migration_marker_path,
    verify_imported_migration,
)
from enoch.private_state import migrate_private_state
from tests.test_enoch_agent_identity import _identity


REVISION = "a" * 40
PROVIDERS = {
    "chat": "slack",
    "runtime": "codex",
    "vcs": "git",
    "forge": "github",
    "service": "launchd",
}


class EnochMigrationTests(unittest.TestCase):
    def test_export_uses_real_clean_git_body_revision(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            bundle = base / "enoch-migration.zip"
            source.mkdir()
            (source / ".gitignore").write_text(".enoch/\n", encoding="utf-8")
            (source / "README.md").write_text("body\n", encoding="utf-8")
            _git(source, "init")
            _git(source, "config", "user.email", "test@example.com")
            _git(source, "config", "user.name", "Migration Test")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "body")
            revision = _git(source, "rev-parse", "HEAD")
            _seed_source(source)

            result = export_migration_bundle(bundle, source)

            self.assertEqual(result.body_revision, revision)
            self.assertTrue(source_migration_marker_path(source).exists())
            self.assertIn(result.migration_id, migration_status(source))
            self.assertIn("status: exported", migration_status(source))

    def test_export_captures_portable_state_and_fences_source(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)

            with _migration_environment():
                result = export_migration_bundle(bundle, source)
                inspection = inspect_migration_bundle(bundle)

            self.assertTrue(bundle.exists())
            self.assertEqual(os.stat(bundle).st_mode & 0o777, 0o600)
            self.assertEqual(result.migration_id, inspection.migration_id)
            self.assertEqual(inspection.body_revision, REVISION)
            self.assertEqual(inspection.provider_bindings, PROVIDERS)
            self.assertFalse(inspection.artifacts_included)
            self.assertTrue(source_migration_marker_path(source).exists())
            archive_paths = {
                item["archive_path"]
                for item in inspection.manifest["contents"]["files"]
            }
            self.assertIn("state/self.json", archive_paths)
            self.assertIn("state/memory/long_term.json", archive_paths)
            self.assertIn("state/task_queue.json", archive_paths)
            self.assertNotIn("state/config.yaml", archive_paths)
            self.assertNotIn("state/daemon_epoch.json", archive_paths)
            self.assertNotIn("state/codex_sessions.json", archive_paths)
            self.assertNotIn("artifacts/task_events.jsonl", archive_paths)
            with zipfile.ZipFile(bundle, "r") as archive:
                bundled_bytes = b"\n".join(
                    archive.read(name)
                    for name in archive.namelist()
                )
            self.assertNotIn(b"source-secret", bundled_bytes)

    def test_artifacts_are_included_only_when_requested(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)

            with _migration_environment():
                export_migration_bundle(bundle, source, include_artifacts=True)
                inspection = inspect_migration_bundle(bundle)

            paths = {
                item["archive_path"]
                for item in inspection.manifest["contents"]["files"]
            }
            self.assertTrue(inspection.artifacts_included)
            self.assertIn("artifacts/task_events.jsonl", paths)

    def test_import_dry_run_is_read_only_then_apply_preserves_target_config(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)
            target.mkdir()
            target_config = target / ".enoch" / "config.yaml"
            target_config.parent.mkdir()
            target_config.write_text(
                'providers:\n  chat: "slack"\nslack:\n  bot_token: "target-secret"\n',
                encoding="utf-8",
            )

            with _migration_environment():
                exported = export_migration_bundle(bundle, source)
                dry_run = import_migration_bundle(bundle, target, dry_run=True)
                self.assertFalse((target / ".enoch" / "self.json").exists())
                imported = import_migration_bundle(bundle, target)

            self.assertTrue(dry_run.dry_run)
            self.assertFalse(dry_run.applied)
            self.assertTrue(imported.applied)
            self.assertEqual(imported.migration_id, exported.migration_id)
            self.assertTrue((target / ".enoch" / "self.json").exists())
            self.assertTrue(target_migration_marker_path(target).exists())
            self.assertIn("target-secret", target_config.read_text(encoding="utf-8"))

    def test_target_authority_advances_then_verification_writes_report(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "enoch-migration.zip"
            _seed_source(source, authority_generation=7)
            target.mkdir()

            with _migration_environment():
                export_migration_bundle(bundle, source)
                import_migration_bundle(bundle, target)
                before = verify_imported_migration(target)
                with self.assertRaisesRegex(AgentMigrationError, "activated and verified"):
                    begin_daemon_epoch(target, provider="slack")
                activation = activate_imported_migration(target)
                after = verify_imported_migration(target)

            self.assertFalse(before.passed)
            self.assertFalse(before.authority_active)
            self.assertEqual(activation.authority_generation, 8)
            self.assertTrue(after.passed)
            self.assertTrue(after.authority_active)
            report = json.loads(after.report_path.read_text(encoding="utf-8"))
            target_marker = json.loads(
                target_migration_marker_path(target).read_text(encoding="utf-8")
            )
            self.assertEqual(report["source_authority_generation"], 7)
            self.assertEqual(report["target_authority_generation"], 8)
            self.assertEqual(report["substrate"]["tasks"]["pending"], [42])
            self.assertEqual(target_marker["status"], "verified")

            epoch_path = target / ".enoch" / "daemon_epoch.json"
            epoch_document = json.loads(epoch_path.read_text(encoding="utf-8"))
            epoch_document["current"]["pid"] = 999999
            epoch_path.write_text(json.dumps(epoch_document), encoding="utf-8")
            return_bundle = base / "return-migration.zip"
            with _migration_environment():
                returned = export_migration_bundle(return_bundle, target)

            self.assertTrue(return_bundle.exists())
            self.assertNotEqual(returned.migration_id, after.migration_id)

    def test_exported_source_cannot_restart_until_explicit_cancel(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)

            with _migration_environment():
                exported = export_migration_bundle(bundle, source)
                with self.assertRaisesRegex(AgentMigrationError, "source instance is fenced"):
                    begin_daemon_epoch(source, provider="slack")
                cancel_exported_migration(exported.migration_id, source)
                epoch = begin_daemon_epoch(source, provider="slack")

            self.assertEqual(epoch.generation, 1)

    def test_export_rejects_running_task_and_host_local_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)
            queue_path = source / ".enoch" / "task_queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["running"] = queue["pending"].pop()
            queue["running"]["status"] = "running"
            queue_path.write_text(json.dumps(queue), encoding="utf-8")

            with _migration_environment():
                with self.assertRaisesRegex(AgentMigrationError, "still running"):
                    export_migration_bundle(bundle, source)

            recovered = queue["running"]
            recovered["status"] = "pending"
            recovered["workspace_path"] = "/source-only/worktree"
            queue["running"] = None
            queue["pending"] = [recovered]
            queue_path.write_text(json.dumps(queue), encoding="utf-8")
            with _migration_environment():
                with self.assertRaisesRegex(AgentMigrationError, "host-local workspace"):
                    export_migration_bundle(bundle, source)

    def test_import_rejects_body_mismatch_and_existing_agent_state(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)
            target.mkdir()

            with _migration_environment(revision=REVISION):
                export_migration_bundle(bundle, source)
            with _migration_environment(revision="b" * 40):
                with self.assertRaisesRegex(AgentMigrationError, "does not match"):
                    import_migration_bundle(bundle, target, dry_run=True)

            existing = target / ".enoch" / "self.json"
            existing.parent.mkdir()
            existing.write_text("{}", encoding="utf-8")
            with _migration_environment(revision=REVISION):
                with self.assertRaisesRegex(AgentMigrationError, "will not overwrite"):
                    import_migration_bundle(bundle, target)

            self.assertEqual(existing.read_text(encoding="utf-8"), "{}")

    def test_failed_import_removes_new_files_and_preserves_target_config(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "enoch-migration.zip"
            _seed_source(source)
            target.mkdir()
            config = target / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text('slack:\n  bot_token: "target-secret"\n', encoding="utf-8")

            from enoch import migration

            real_write = migration._atomic_write_bytes
            writes = 0

            def fail_second_write(path, payload):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("simulated import interruption")
                return real_write(path, payload)

            with _migration_environment():
                export_migration_bundle(bundle, source)
                with patch(
                    "enoch.migration._atomic_write_bytes",
                    side_effect=fail_second_write,
                ):
                    with self.assertRaisesRegex(AgentMigrationError, "newly written"):
                        import_migration_bundle(bundle, target)

            self.assertFalse((target / ".enoch" / "self.json").exists())
            self.assertFalse(target_migration_marker_path(target).exists())
            self.assertIn("target-secret", config.read_text(encoding="utf-8"))

    def test_inspection_rejects_tampered_payload(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            bundle = base / "enoch-migration.zip"
            tampered = base / "tampered.zip"
            _seed_source(source)

            with _migration_environment():
                export_migration_bundle(bundle, source)
            with zipfile.ZipFile(bundle, "r") as original, zipfile.ZipFile(
                tampered, "w", compression=zipfile.ZIP_DEFLATED
            ) as changed:
                for info in original.infolist():
                    payload = original.read(info.filename)
                    if info.filename == "state/self.json":
                        payload += b"tampered"
                    changed.writestr(info.filename, payload)

            with self.assertRaisesRegex(AgentMigrationError, "size mismatch"):
                inspect_migration_bundle(tampered)

    def test_export_requires_current_state_and_external_destination(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".enoch").mkdir()
            inside = root / "bundle.zip"

            with _migration_environment():
                with self.assertRaisesRegex(AgentMigrationError, "outside the software body"):
                    export_migration_bundle(inside, root)


def _seed_source(root: Path, *, authority_generation: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    migrate_private_state(root)
    state = root / ".enoch"
    install_agent_identity(_identity(), root)
    memory = state / "memory" / "long_term.json"
    memory.parent.mkdir()
    memory.write_text(
        json.dumps({"schema_version": 1, "memories": []}),
        encoding="utf-8",
    )
    (state / "task_queue.json").write_text(
        json.dumps(
            {
                "schema_version": 15,
                "next_id": 43,
                "pending": [
                    {
                        "id": 42,
                        "chat_id": "C123",
                        "text": "continue after migration",
                        "created_at": "2026-09-07T00:00:00+00:00",
                    }
                ],
                "paused": [],
                "running": None,
                "history": [],
                "reconciliation": None,
            }
        ),
        encoding="utf-8",
    )
    (state / "config.yaml").write_text(
        'providers:\n  chat: "slack"\nslack:\n  bot_token: "source-secret"\n',
        encoding="utf-8",
    )
    (state / "codex_sessions.json").write_text(
        json.dumps({"schema_version": 1, "sessions": {"chat:C123": {"session_id": "native"}}}),
        encoding="utf-8",
    )
    if authority_generation:
        (state / "daemon_epoch.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "current": {
                        "token": "old-token",
                        "generation": authority_generation,
                        "provider": "slack",
                        "pid": 999999,
                        "started_at": "2026-09-07T00:00:00+00:00",
                    },
                }
            ),
            encoding="utf-8",
        )
    artifact = state / "artifacts" / "task_events.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"task_id":42}\n', encoding="utf-8")


def _migration_environment(*, revision: str = REVISION):
    return _CombinedPatches(
        patch("enoch.migration.ensure_clean_worktree"),
        patch("enoch.migration.current_repository_revision", return_value=revision),
        patch("enoch.migration._provider_bindings", return_value=dict(PROVIDERS)),
    )


class _CombinedPatches:
    def __init__(self, *patchers) -> None:
        self.patchers = patchers

    def __enter__(self):
        return tuple(patcher.start() for patcher in self.patchers)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
