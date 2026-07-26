from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.private_state import (
    MANIFEST_NAME,
    PRIVATE_STATE_MANIFEST_SCHEMA_VERSION,
    PRIVATE_STATE_VERSION,
    PrivateStateMigrationError,
    UnsupportedPrivateStateError,
    assert_private_state_supported,
    migrate_private_state,
    plan_private_state,
    private_state_manifest_path,
)


class EnochPrivateStateTests(unittest.TestCase):
    def test_empty_state_dry_run_is_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            plan = plan_private_state(root)
            result = migrate_private_state(root, dry_run=True)

            self.assertTrue(plan.valid)
            self.assertEqual(plan.manifest_status, "missing")
            self.assertTrue(plan.migration_required)
            self.assertFalse(result.applied)
            self.assertTrue(result.dry_run)
            self.assertFalse((root / ".enoch").exists())

    def test_migration_backs_up_normalizes_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".enoch"
            state.mkdir()
            queue = state / "task_queue.json"
            queue.write_text(
                json.dumps(
                    {
                        "schema_version": 10,
                        "next_id": 2,
                        "pending": [
                            {
                                "id": 1,
                                "chat_id": 42,
                                "text": "legacy task",
                                "created_at": "2026-07-25T00:00:00+00:00",
                            }
                        ],
                        "running": None,
                        "history": [],
                    }
                ),
                encoding="utf-8",
            )
            last_input = state / "last_codex_input.json"
            last_input.write_text(
                json.dumps({"prompt": "legacy input"}),
                encoding="utf-8",
            )

            result = migrate_private_state(root)
            second = migrate_private_state(root)

            migrated_queue = json.loads(queue.read_text(encoding="utf-8"))
            migrated_input = json.loads(last_input.read_text(encoding="utf-8"))
            manifest = json.loads(
                private_state_manifest_path(root).read_text(encoding="utf-8")
            )
            backup_queue = json.loads(
                (result.backup_path / "task_queue.json").read_text(encoding="utf-8")
            )

        self.assertTrue(result.applied)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(migrated_queue["schema_version"], 12)
        self.assertEqual(migrated_queue["paused"], [])
        self.assertEqual(migrated_queue["pending"][0]["required_capabilities"], [])
        self.assertEqual(migrated_input["schema_version"], 1)
        self.assertEqual(manifest["schema_version"], PRIVATE_STATE_MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest["state_version"], PRIVATE_STATE_VERSION)
        self.assertEqual(backup_queue["schema_version"], 10)
        self.assertFalse(second.applied)
        self.assertFalse(second.plan.migration_required)

    def test_future_file_schema_is_rejected_without_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / ".enoch" / "task_queue.json"
            queue.parent.mkdir()
            original = json.dumps(
                {
                    "schema_version": 99,
                    "pending": [],
                    "paused": [],
                    "running": None,
                    "history": [],
                }
            )
            queue.write_text(original, encoding="utf-8")

            plan = plan_private_state(root)
            with self.assertRaises(UnsupportedPrivateStateError):
                assert_private_state_supported(root)
            with self.assertRaises(UnsupportedPrivateStateError):
                migrate_private_state(root)

            self.assertFalse(plan.valid)
            self.assertIn("unsupported schema version 99", "\n".join(plan.errors))
            self.assertEqual(queue.read_text(encoding="utf-8"), original)
            self.assertFalse(private_state_manifest_path(root).exists())

    def test_future_manifest_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / ".enoch" / MANIFEST_NAME
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": PRIVATE_STATE_MANIFEST_SCHEMA_VERSION,
                        "state_version": PRIVATE_STATE_VERSION + 1,
                        "schemas": {},
                    }
                ),
                encoding="utf-8",
            )

            plan = plan_private_state(root)

        self.assertFalse(plan.valid)
        self.assertIn("unsupported private-state version", "\n".join(plan.errors))

    def test_corrupt_state_is_preserved_and_not_backed_up(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / ".enoch" / "task_queue.json"
            queue.parent.mkdir()
            queue.write_text('{"pending": [', encoding="utf-8")

            with self.assertRaises(UnsupportedPrivateStateError):
                migrate_private_state(root)

            self.assertEqual(queue.read_text(encoding="utf-8"), '{"pending": [')
            self.assertFalse((root / ".enoch" / "backups").exists())

    def test_failed_manifest_commit_restores_migrated_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / ".enoch" / "task_queue.json"
            queue.parent.mkdir()
            original = json.dumps(
                {
                    "schema_version": 10,
                    "pending": [],
                    "running": None,
                    "history": [],
                }
            )
            queue.write_text(original, encoding="utf-8")

            from enoch import private_state

            real_atomic_write = private_state.atomic_write

            def fail_manifest(path, text):
                if path.name == MANIFEST_NAME:
                    raise OSError("simulated manifest failure")
                return real_atomic_write(path, text)

            with patch("enoch.private_state.atomic_write", side_effect=fail_manifest):
                with self.assertRaises(PrivateStateMigrationError) as raised:
                    migrate_private_state(root)

            self.assertIn("rolled back", str(raised.exception))
            self.assertEqual(queue.read_text(encoding="utf-8"), original)
            self.assertFalse(private_state_manifest_path(root).exists())
            self.assertEqual(len(list((root / ".enoch" / "backups").iterdir())), 1)

    def test_failed_post_migration_validation_rolls_back(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            last_input = root / ".enoch" / "last_codex_input.json"
            last_input.parent.mkdir()
            original = json.dumps({"prompt": "legacy input"})
            last_input.write_text(original, encoding="utf-8")
            stale_manifest = {
                "schema_version": PRIVATE_STATE_MANIFEST_SCHEMA_VERSION,
                "state_version": PRIVATE_STATE_VERSION,
                "schemas": {},
            }

            with patch(
                "enoch.private_state._current_manifest",
                return_value=stale_manifest,
            ):
                with self.assertRaises(PrivateStateMigrationError) as raised:
                    migrate_private_state(root)

            self.assertIn("rolled back", str(raised.exception))
            self.assertEqual(last_input.read_text(encoding="utf-8"), original)
            self.assertFalse(private_state_manifest_path(root).exists())

    def test_live_daemon_blocks_apply_but_not_dry_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            epoch = root / ".enoch" / "daemon_epoch.json"
            epoch.parent.mkdir()
            epoch.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "current": {
                            "token": "active",
                            "generation": 1,
                            "provider": "chat",
                            "pid": os.getpid(),
                            "started_at": "2026-07-25T00:00:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )

            dry_run = migrate_private_state(root, dry_run=True)
            with self.assertRaises(PrivateStateMigrationError):
                migrate_private_state(root)

        self.assertTrue(dry_run.dry_run)
        self.assertFalse(dry_run.applied)

    def test_artifact_files_are_outside_private_state_migration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / ".enoch" / "artifacts" / "task_events.jsonl"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("not-json\n", encoding="utf-8")

            plan = plan_private_state(root)

        self.assertTrue(plan.valid)
        self.assertEqual(plan.files_checked, 0)
