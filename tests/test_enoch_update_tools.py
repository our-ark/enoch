from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


from enoch.update_tools import schedule_daemon_restart, schedule_daemon_stop


class EnochUpdateToolsTests(unittest.TestCase):
    @patch("enoch.update_tools.load_provider")
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

    @patch("enoch.update_tools.load_provider")
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


if __name__ == "__main__":
    unittest.main()
