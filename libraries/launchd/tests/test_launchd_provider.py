from pathlib import Path
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


LIBRARY = Path(__file__).resolve().parents[1]
REPOSITORY = LIBRARY.parents[1]
sys.path.insert(0, str(REPOSITORY / "libraries" / "provider-kit" / "src"))
sys.path.insert(0, str(LIBRARY / "src"))

from our_ark_launchd import LABEL, LaunchdServiceProvider, plist_bytes


class LaunchdServiceProviderTests(unittest.TestCase):
    def test_manifest_runs_enoch_agent_from_the_selected_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            provider = LaunchdServiceProvider(home=base / "home")
            paths = provider.paths(base / "Enoch Instance")

            payload = plistlib.loads(plist_bytes(paths))

        self.assertEqual(
            payload["ProgramArguments"],
            [str(paths.root / "bin" / "enoch-agent")],
        )
        self.assertEqual(payload["WorkingDirectory"], str(paths.root))
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["StandardOutPath"], str(paths.stdout))
        self.assertEqual(payload["StandardErrorPath"], str(paths.stderr))

    @patch("our_ark_launchd.platform.system", return_value="Darwin")
    @patch("our_ark_launchd.subprocess.run")
    def test_install_writes_manifest_and_bootstraps_user_service(
        self,
        run: MagicMock,
        _system: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            provider = LaunchdServiceProvider(home=base / "home")

            result = provider.install(base / "repo")
            paths = provider.paths(base / "repo")

            self.assertTrue(paths.plist.exists())

        self.assertIn(str(paths.plist), result)
        run.assert_called_once_with(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(paths.plist)],
            capture_output=True,
            text=True,
        )

    @patch("our_ark_launchd.subprocess.Popen")
    def test_scheduled_restart_uses_daemon_launcher(
        self,
        popen: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            LaunchdServiceProvider().schedule_restart(root)

        popen.assert_called_once_with(
            [str(root / "bin" / "enoch-daemon"), "restart"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def test_descriptor_identifies_launchd_service(self) -> None:
        provider = LaunchdServiceProvider()
        self.assertEqual(provider.name, "launchd")
        self.assertEqual(provider.provider_kind, "service")
        self.assertEqual(LABEL, "com.ourark.enoch")


if __name__ == "__main__":
    unittest.main()
