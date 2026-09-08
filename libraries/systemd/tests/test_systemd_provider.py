from dataclasses import replace
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

from our_ark_provider_kit import ProviderContractConformanceMixin, ServiceProvider, ServiceProviderError
from our_ark_systemd import UNIT_NAME, SystemdServiceProvider, unit_text


class SystemdServiceProviderTests(ProviderContractConformanceMixin, unittest.TestCase):
    provider_kind = "service"
    provider_protocol = ServiceProvider

    def create_provider(self, root: Path) -> SystemdServiceProvider:
        return SystemdServiceProvider(home=root / "home")

    def test_manifest_runs_enoch_agent_as_a_resilient_user_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Enoch Instance"
            _write_agent_body(root)
            provider = SystemdServiceProvider(home=base / "home")
            paths = provider.paths(root)

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
            root = base / "repo"
            _write_agent_body(root)
            provider = SystemdServiceProvider(home=base / "home")

            result = provider.install(root)
            paths = provider.paths(root)

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
                    ["systemctl", "--user", "enable", "--now", paths.unit_name],
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_agent_body(root)
            provider = SystemdServiceProvider(home=root / "home")
            paths = provider.paths(root)
            provider.schedule_restart(root)

        popen.assert_called_once_with(
            ["systemctl", "--user", "restart", paths.unit_name],
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

    @patch("our_ark_systemd.platform.system", return_value="Linux")
    @patch("our_ark_systemd.shutil.which", return_value="/usr/bin/systemctl")
    @patch("our_ark_systemd.subprocess.run")
    def test_two_instances_have_independent_manifests_and_lifecycle(self, run, _which, _system) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first, second = base / "work", base / "life"
            for root in (first, second):
                _write_agent_body(root)
            provider = SystemdServiceProvider(home=base / "home")
            provider.start(first)
            provider.start(second)
            a, b = provider.paths(first), provider.paths(second)
            self.assertNotEqual(a.unit_name, b.unit_name)
            self.assertNotEqual(a.unit, b.unit)
            self.assertIn(f'ExecStart="{second.resolve() / "bin" / "enoch-agent"}"', b.unit.read_text())
            second_manifest = b.unit.read_bytes()
            run.reset_mock()
            provider.stop(first)
            provider.restart(second)
            provider.status(second)
            provider.logs(second)
            provider.uninstall(first)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["systemctl", "--user", "stop", a.unit_name], commands)
            self.assertIn(["systemctl", "--user", "restart", b.unit_name], commands)
            self.assertIn(["systemctl", "--user", "is-active", "--quiet", b.unit_name], commands)
            self.assertIn(["journalctl", "--user", "-u", b.unit_name, "-n", "80", "--no-pager"], commands)
            self.assertFalse(a.unit.exists())
            self.assertEqual(b.unit.read_bytes(), second_manifest)

    def test_existing_legacy_service_is_used_only_by_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first, second = base / "work", base / "life"
            for root in (first, second):
                _write_agent_body(root)
            provider = SystemdServiceProvider(home=base / "home")
            paths = provider.paths(first)
            legacy = replace(paths, unit_name=UNIT_NAME, unit=paths.unit_directory / UNIT_NAME)
            provider._write_manifest(legacy)
            self.assertEqual(provider.paths(first).unit_name, UNIT_NAME)
            self.assertNotEqual(provider.paths(second).unit_name, UNIT_NAME)

    def test_service_identity_is_stable_across_symlink_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, alias = base / "work", base / "alias"
            _write_agent_body(root)
            alias.symlink_to(root, target_is_directory=True)
            provider = SystemdServiceProvider(home=base / "home")
            self.assertEqual(provider.paths(root), provider.paths(alias))

    def test_foreign_scoped_manifest_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_agent_body(root)
            provider = SystemdServiceProvider(home=root / "home")
            paths = provider.paths(root)
            paths.unit.parent.mkdir(parents=True)
            paths.unit.write_text('[Service]\nWorkingDirectory="/another/agent"\n')
            with self.assertRaisesRegex(ServiceProviderError, "another installation"):
                provider.paths(root)

    def test_legacy_manifest_with_overridden_working_directory_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_agent_body(root)
            provider = SystemdServiceProvider(home=root / "home")
            paths = provider.paths(root)
            legacy = paths.unit_directory / UNIT_NAME
            legacy.parent.mkdir(parents=True)
            text = unit_text(paths).replace('[Install]', 'WorkingDirectory="/another/agent"\n[Install]')
            legacy.write_text(text)
            self.assertNotEqual(provider.paths(root).unit, legacy)
            self.assertEqual(legacy.read_text(), text)

    def test_descendant_package_keeps_its_own_launcher_and_service_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _write_agent_body(root, package="noah", name="Noah")
            paths = SystemdServiceProvider(home=root / "home").paths(root)
            self.assertTrue(paths.unit_name.startswith("our-ark-noah-"))
            self.assertIn(f'ExecStart="{root / "bin" / "noah-agent"}"', unit_text(paths))


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
