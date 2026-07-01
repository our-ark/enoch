from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch import daemon


class EnochDaemonTests(unittest.TestCase):
    def test_plist_runs_telegram_bridge_with_keepalive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            paths = daemon.daemon_paths(root=root, home=home)

            payload = plistlib.loads(daemon.plist_bytes(paths))

        self.assertEqual(payload["Label"], "com.ourark.enoch")
        resolved_root = root.resolve()
        self.assertEqual(payload["ProgramArguments"], [str(resolved_root / "bin" / "enoch-telegram")])
        self.assertEqual(payload["WorkingDirectory"], str(resolved_root))
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["EnvironmentVariables"]["PYTHONPATH"], str(resolved_root / "src"))
        self.assertEqual(payload["EnvironmentVariables"]["ENOCH_PYTHON"], sys.executable)

    @patch("enoch.daemon.platform.system", return_value="Darwin")
    @patch("enoch.daemon._launchctl")
    def test_install_writes_launch_agent(self, launchctl: MagicMock, _system: MagicMock) -> None:
        launchctl.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            _write_config(root)
            with patch("enoch.daemon.Path.home", return_value=home):
                message = daemon.install(root=root)

            plist = home / "Library" / "LaunchAgents" / "com.ourark.enoch.plist"
            logs = root / ".enoch" / "logs" / "daemon"

            self.assertTrue(plist.exists())
            self.assertTrue(logs.exists())
            self.assertIn(str(plist), message)
            launchctl.assert_called_once()

    @patch("enoch.daemon.platform.system", return_value="Darwin")
    @patch("enoch.daemon._launchctl")
    def test_status_reports_loaded(self, launchctl: MagicMock, _system: MagicMock) -> None:
        launchctl.return_value.returncode = 0

        self.assertEqual(daemon.status(), "Enoch daemon is loaded.")

    @patch("enoch.daemon.platform.system", return_value="Darwin")
    @patch("enoch.daemon._launchctl")
    def test_doctor_reports_checks(self, launchctl: MagicMock, _system: MagicMock) -> None:
        launchctl.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            paths = daemon.daemon_paths(root=root, home=home)
            _write_config(root)
            paths.logs.mkdir(parents=True)
            paths.launch_agents.mkdir(parents=True)
            paths.plist.write_text("plist", encoding="utf-8")

            with patch("enoch.daemon.Path.home", return_value=home):
                result = daemon.doctor(root=root)

        self.assertIn("Enoch daemon plumbing doctor:", result)
        self.assertIn("- config: ok", result)
        self.assertIn("- launchd: ok", result)

    @patch("enoch.daemon.platform.system", return_value="Darwin")
    @patch("enoch.daemon._launchctl")
    def test_doctor_reports_launchd_not_loaded(self, launchctl: MagicMock, _system: MagicMock) -> None:
        launchctl.return_value.returncode = 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            _write_config(root)
            paths = daemon.daemon_paths(root=root, home=home)
            paths.logs.mkdir(parents=True)
            paths.launch_agents.mkdir(parents=True)
            paths.plist.write_text("plist", encoding="utf-8")

            with patch("enoch.daemon.Path.home", return_value=home):
                result = daemon.doctor(root=root)

        self.assertIn("- launchd: needs attention", result)

    @patch("enoch.daemon.platform.system", return_value="Linux")
    def test_daemon_requires_macos(self, _system: MagicMock) -> None:
        with self.assertRaisesRegex(daemon.DaemonError, "macOS launchd"):
            daemon.start(root=ROOT)

    @patch("enoch.daemon.platform.system", return_value="Darwin")
    def test_start_requires_local_config_token(self, _system: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(daemon.DaemonError, "setup-token"):
                daemon.start(root=Path(directory))


def _write_config(root: Path) -> None:
    (root / ".enoch").mkdir()
    (root / ".enoch" / "config.yaml").write_text(
        "\n".join(["telegram:", '  bot_token: "token"']),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
