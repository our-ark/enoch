from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


from enoch import daemon
from enoch.providers.contracts import ServiceProviderError


class _Service:
    name = "test-service"
    provider_kind = "service"

    def __init__(self) -> None:
        self.calls = []

    def install(self, root=None):
        self.calls.append(("install", root))
        return "installed"

    def uninstall(self, root=None):
        self.calls.append(("uninstall", root))
        return "uninstalled"

    def start(self, root=None):
        self.calls.append(("start", root))
        return "started"

    def stop(self, root=None, *, allow_missing=False):
        self.calls.append(("stop", root, allow_missing))
        return "stopped"

    def restart(self, root=None):
        self.calls.append(("restart", root))
        return "restarted"

    def status(self, root=None):
        return "running"

    def logs(self, root=None, *, lines=80):
        return f"logs:{lines}"

    def doctor(self, root=None):
        return "Service provider: test-service\n- service: ok"

    def manifest(self, root=None):
        return "service manifest"


class EnochDaemonTests(unittest.TestCase):
    def test_install_validates_chat_and_delegates_to_service(self) -> None:
        service = _Service()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("enoch.daemon._require_daemon_config") as require_config,
                patch("enoch.daemon._service", return_value=service),
            ):
                result = daemon.install(root)

        self.assertEqual(result, "installed")
        require_config.assert_called_once_with(root.resolve())
        self.assertEqual(service.calls, [("install", root.resolve())])

    def test_restart_validates_chat_and_uses_atomic_provider_restart(self) -> None:
        service = _Service()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("enoch.daemon._require_daemon_config"),
                patch("enoch.daemon._service", return_value=service),
            ):
                result = daemon.restart(root)

        self.assertEqual(result, "restarted")
        self.assertEqual(service.calls, [("restart", root.resolve())])

    def test_dispatch_routes_status_logs_and_manifest(self) -> None:
        service = _Service()
        with patch("enoch.daemon._service", return_value=service):
            self.assertEqual(daemon.dispatch("status"), "running")
            self.assertEqual(daemon.dispatch("logs", lines=12), "logs:12")
            self.assertEqual(daemon.dispatch("manifest"), "service manifest")

    def test_doctor_combines_chat_and_service_readiness(self) -> None:
        service = _Service()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("enoch.daemon._has_daemon_config", return_value=True),
                patch("enoch.daemon.provider_name", return_value="test-chat"),
                patch("enoch.daemon._service", return_value=service),
            ):
                result = daemon.doctor(root)

        self.assertIn("Enoch service doctor:", result)
        self.assertIn("- config: ok", result)
        self.assertIn("Service provider: test-service", result)

    @patch("builtins.print")
    @patch("enoch.daemon.dispatch", side_effect=ServiceProviderError("service unavailable"))
    def test_main_reports_service_provider_errors(self, _dispatch: MagicMock, print_: MagicMock) -> None:
        with self.assertRaises(SystemExit):
            daemon.main(["status"])

        print_.assert_called_once_with("service unavailable")

    def test_start_requires_configured_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("enoch.daemon._has_daemon_config", return_value=False):
                with self.assertRaisesRegex(daemon.DaemonError, "Configure the selected chat provider"):
                    daemon.start(root)


if __name__ == "__main__":
    unittest.main()
