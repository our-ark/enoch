from dataclasses import replace
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
from our_ark_provider_kit import ProviderContractConformanceMixin, ServiceProvider, ServiceProviderError


class LaunchdServiceProviderTests(ProviderContractConformanceMixin, unittest.TestCase):
    provider_kind = "service"
    provider_protocol = ServiceProvider

    def create_provider(self, root: Path) -> LaunchdServiceProvider:
        return LaunchdServiceProvider(home=root / "home")

    def test_manifest_runs_enoch_agent_from_the_selected_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Enoch Instance"
            _write_agent_body(root)
            provider = LaunchdServiceProvider(home=base / "home")
            paths = provider.paths(root)

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
            root = base / "repo"
            _write_agent_body(root)
            provider = LaunchdServiceProvider(home=base / "home")

            result = provider.install(root)
            paths = provider.paths(root)

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
            _write_agent_body(root)
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

    @patch("our_ark_launchd.platform.system", return_value="Darwin")
    @patch("our_ark_launchd.subprocess.run")
    def test_two_instances_have_independent_manifests_and_lifecycle(self, run, _system) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first, second = base / "work", base / "life"
            for root in (first, second):
                _write_agent_body(root)
            provider = LaunchdServiceProvider(home=base / "home")
            provider.start(first)
            provider.start(second)
            a, b = provider.paths(first), provider.paths(second)
            self.assertNotEqual(a.label, b.label)
            self.assertNotEqual(a.plist, b.plist)
            self.assertNotEqual(a.logs, b.logs)
            self.assertEqual(plistlib.loads(b.plist.read_bytes())["ProgramArguments"],
                             [str(second.resolve() / "bin" / "enoch-agent")])
            second_manifest = b.plist.read_bytes()
            run.reset_mock()
            provider.stop(first)
            provider.restart(second)
            provider.status(second)
            provider.uninstall(first)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ["launchctl", "bootout", f"gui/{os.getuid()}", str(a.plist)])
            self.assertIn(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{b.label}"], commands)
            self.assertIn(["launchctl", "print", f"gui/{os.getuid()}/{b.label}"], commands)
            self.assertFalse(a.plist.exists())
            self.assertEqual(b.plist.read_bytes(), second_manifest)

    def test_existing_legacy_service_is_used_only_by_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first, second = base / "work", base / "life"
            for root in (first, second):
                _write_agent_body(root)
            provider = LaunchdServiceProvider(home=base / "home")
            paths = provider.paths(first)
            legacy = replace(paths, label=LABEL, plist=paths.launch_agents / f"{LABEL}.plist")
            provider._write_manifest(legacy)
            self.assertEqual(provider.paths(first).label, LABEL)
            self.assertNotEqual(provider.paths(second).label, LABEL)
            self.assertEqual(provider.paths(first).plist, legacy.plist)

    def test_service_identity_is_stable_across_symlink_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, alias = base / "work", base / "alias"
            _write_agent_body(root)
            alias.symlink_to(root, target_is_directory=True)
            provider = LaunchdServiceProvider(home=base / "home")
            self.assertEqual(provider.paths(root), provider.paths(alias))

    def test_foreign_scoped_manifest_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "work"
            _write_agent_body(root)
            provider = LaunchdServiceProvider(home=base / "home")
            paths = provider.paths(root)
            paths.plist.parent.mkdir(parents=True)
            paths.plist.write_bytes(plistlib.dumps({"WorkingDirectory": "/another/agent"}))
            with self.assertRaisesRegex(ServiceProviderError, "another installation"):
                provider.paths(root)

    def test_malformed_legacy_manifest_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_agent_body(root)
            provider = LaunchdServiceProvider(home=root / "home")
            paths = provider.paths(root)
            legacy = paths.launch_agents / f"{LABEL}.plist"
            legacy.parent.mkdir(parents=True)
            for invalid in (b"not a plist", b'<?xml version="1.0"?><plist><dict>'):
                with self.subTest(invalid=invalid):
                    legacy.write_bytes(invalid)
                    self.assertNotEqual(provider.paths(root).plist, legacy)
                    self.assertEqual(legacy.read_bytes(), invalid)

    def test_descendant_package_keeps_its_own_launcher_and_service_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _write_agent_body(root, package="noah", name="Noah")
            paths = LaunchdServiceProvider(home=root / "home").paths(root)
            payload = plistlib.loads(plist_bytes(paths))
            self.assertTrue(paths.label.startswith("com.ourark.noah."))
            self.assertEqual(payload["ProgramArguments"], [str(root / "bin" / "noah-agent")])
            self.assertEqual(paths.logs, root / ".noah" / "logs" / "daemon")


def _write_agent_body(root: Path, package: str = "enoch", name: str = "Enoch") -> None:
    (root / "src" / package).mkdir(parents=True)
    (root / "genesis.toml").write_text(
        f'package = "{package}"\n',
        encoding="utf-8",
    )
    (root / "src" / package / "identity.yaml").write_text(
        f'name: "{name}"\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
