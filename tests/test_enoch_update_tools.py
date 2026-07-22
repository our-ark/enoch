from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch


from enoch.operations.update_tools import schedule_daemon_restart, schedule_daemon_stop
from enoch.providers.vcs import GitVersionControlProvider


class EnochUpdateToolsTests(unittest.TestCase):
    def test_git_provider_updates_and_restores_authoritative_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote = base / "remote.git"
            root = base / "instance"
            writer = base / "writer"
            self._git(base, "init", "--bare", str(remote))
            self._git(base, "init", "-b", "main", str(root))
            self._configure_git(root)
            (root / "state.txt").write_text("one\n", encoding="utf-8")
            self._git(root, "add", "state.txt")
            self._git(root, "commit", "-m", "initial")
            initial = self._git(root, "rev-parse", "HEAD")
            self._git(root, "remote", "add", "origin", str(remote))
            self._git(root, "push", "-u", "origin", "main")

            self._git(base, "clone", "--branch", "main", str(remote), str(writer))
            self._configure_git(writer)
            (writer / "state.txt").write_text("two\n", encoding="utf-8")
            self._git(writer, "commit", "-am", "advance")
            self._git(writer, "push", "origin", "main")

            provider = GitVersionControlProvider()
            provider.refresh_authoritative(root)
            latest = provider.authoritative_revision(root)

            self.assertNotEqual(initial, latest)
            self.assertEqual(provider.current_revision(root), initial)
            self.assertTrue(provider.is_ancestor(initial, latest, root))
            provider.update_to_authoritative(root)
            self.assertEqual(provider.current_revision(root), latest)
            provider.restore_revision(initial, root)
            self.assertEqual(provider.current_revision(root), initial)

    @patch("enoch.operations.update_tools.load_provider")
    def test_scheduled_restart_uses_selected_service_provider(
        self,
        load_provider: MagicMock,
    ) -> None:
        service = load_provider.return_value
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            schedule_daemon_restart(root)

        load_provider.assert_called_once_with("service", root)
        service.schedule_restart.assert_called_once_with(root)

    @patch("enoch.operations.update_tools.load_provider")
    def test_scheduled_stop_uses_selected_service_provider(
        self,
        load_provider: MagicMock,
    ) -> None:
        service = load_provider.return_value
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            schedule_daemon_stop(root)

        load_provider.assert_called_once_with("service", root)
        service.schedule_stop.assert_called_once_with(root)

    @staticmethod
    def _configure_git(root: Path) -> None:
        EnochUpdateToolsTests._git(root, "config", "user.name", "Enoch Tests")
        EnochUpdateToolsTests._git(root, "config", "user.email", "enoch@example.test")

    @staticmethod
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
