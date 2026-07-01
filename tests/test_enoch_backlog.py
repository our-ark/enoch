from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import (
    add_backlog_item,
    backlog_status,
    next_backlog_item,
    promote_next_backlog_item,
    remove_backlog_item,
    reprioritize_backlog_item,
)


class EnochBacklogTests(unittest.TestCase):
    def test_add_backlog_item_persists_priority_and_context(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            item = add_backlog_item(
                42,
                "do it later",
                root,
                priority="p0",
                context="Use the frozen context.",
                context_source="chat-snapshot",
            )
            status = backlog_status(root)

        self.assertEqual(item.id, 1)
        self.assertEqual(item.priority, "p0")
        self.assertEqual(status.pending, (item,))
        self.assertEqual(status.pending[0].context, "Use the frozen context.")
        self.assertEqual(status.pending[0].context_source, "chat-snapshot")

    def test_promote_next_uses_priority_then_fifo(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            p1 = add_backlog_item(42, "p1 first", root, priority="p1")
            p2 = add_backlog_item(42, "p2 first", root, priority="p2")
            first_p0 = add_backlog_item(42, "p0 first", root, priority="p0")
            second_p0 = add_backlog_item(42, "p0 second", root, priority="p0")

            promoted = [
                promote_next_backlog_item(root, promoted_task_id=10),
                promote_next_backlog_item(root, promoted_task_id=11),
                promote_next_backlog_item(root, promoted_task_id=12),
                promote_next_backlog_item(root, promoted_task_id=13),
                promote_next_backlog_item(root),
            ]
            status = backlog_status(root)

        self.assertEqual([item.id for item in promoted if item is not None], [first_p0.id, second_p0.id, p1.id, p2.id])
        self.assertIsNone(promoted[-1])
        self.assertEqual(status.pending, ())
        self.assertEqual([item.promoted_task_id for item in status.history], [10, 11, 12, 13])

    def test_next_backlog_item_uses_priority_without_removing_item(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "p2 first", root, priority="p2")
            p0 = add_backlog_item(42, "p0 first", root, priority="p0")

            item = next_backlog_item(root)
            status = backlog_status(root)

        self.assertEqual(item.id, p0.id)
        self.assertEqual(status.pending_count, 2)

    def test_remove_backlog_item_moves_pending_item_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            item = add_backlog_item(42, "drop this", root)

            removed = remove_backlog_item(item.id, root)
            status = backlog_status(root)

        self.assertEqual(removed.id, item.id)
        self.assertEqual(removed.status, "removed")
        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].id, item.id)

    def test_reprioritize_backlog_item_updates_pending_item(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            item = add_backlog_item(42, "raise this", root, priority="p2")

            updated = reprioritize_backlog_item(item.id, "p0", root)
            status = backlog_status(root)

        self.assertEqual(updated.priority, "p0")
        self.assertEqual(status.pending[0].priority, "p0")


if __name__ == "__main__":
    unittest.main()
