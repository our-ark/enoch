from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch


LIBRARY = Path(__file__).resolve().parents[1]
REPOSITORY = LIBRARY.parents[1]
sys.path.insert(0, str(REPOSITORY / "libraries" / "provider-kit" / "src"))
sys.path.insert(0, str(LIBRARY / "src"))

from our_ark_systemd import UNIT_NAME, SystemdServiceProvider, unit_text


class SystemdServiceProviderTests(unittest.TestCase):
    def test_manifest_runs_enoch_agent_as_a_resilient_user_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            provider = SystemdServiceProvider(home=base / "home")
            paths = provider.paths(base / "Enoch Instance")

            manifest = unit_text(paths)

        self.assertIn(f'WorkingDirectory="{paths.root}"', manifest)
        self.assertIn(f'ExecStart="{paths.root / "bin" / "enoch-agent"}"', manifest)
        self.assertIn("Restart=always", manifest)
        self.assertIn("WantedBy=default.target", manifest)
        self.assertIn(f'Environment="PYTHONPATH={paths.root / "src"}"', manifest)

    @patch("our_ark_systemd.platform.system", return_value="Linux")
    @patch("our_ark_systemd.shutil.which", return_value="/usr/bin/systemctl")
    @patch("our_ark_systemd.subprocess.run")
    def test_install_writes_unit_and_enables_user_service(
        self,
        run: MagicMock,
        _which: MagicMock,
        _system: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            provider = SystemdServiceProvider(home=base / "home")

            result = provider.install(base / "repo")
            paths = provider.paths(base / "repo")

            self.assertTrue(paths.unit.exists())

        self.assertIn(str(paths.unit), result)
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
                call(
                    ["systemctl", "--user", "enable", "--now", UNIT_NAME],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
            ],
        )

    @patch("our_ark_systemd.subprocess.Popen")
    def test_scheduled_restart_uses_systemctl_user_service(
        self,
        popen: MagicMock,
    ) -> None:
        SystemdServiceProvider().schedule_restart()

        popen.assert_called_once_with(
            ["systemctl", "--user", "restart", UNIT_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @patch("our_ark_systemd.platform.system", return_value="Linux")
    @patch("our_ark_systemd.shutil.which", return_value=None)
    def test_missing_systemctl_is_reported_as_a_host_error(
        self,
        _which: MagicMock,
        _system: MagicMock,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "systemctl is not available"):
            SystemdServiceProvider().start()


if __name__ == "__main__":
    unittest.main()
