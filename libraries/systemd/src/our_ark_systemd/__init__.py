from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from our_ark_provider_kit import ServiceProviderError


UNIT_NAME = "our-ark-enoch.service"


def supports_host() -> bool:
    return platform.system() == "Linux"


@dataclass(frozen=True)
class SystemdPaths:
    root: Path
    unit_directory: Path
    unit: Path


class SystemdServiceProvider:
    name = "systemd"
    provider_kind = "service"

    def __init__(self, *, home: Path | None = None) -> None:
        self.home = home

    def install(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._write_manifest(paths)
        self._systemctl(["daemon-reload"])
        self._systemctl(["enable", "--now", UNIT_NAME])
        return f"Enoch service installed at {paths.unit}."

    def uninstall(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._systemctl(["disable", "--now", UNIT_NAME], allow_failure=True)
        paths.unit.unlink(missing_ok=True)
        self._systemctl(["daemon-reload"])
        return "Enoch service uninstalled."

    def start(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._write_manifest(paths)
        self._systemctl(["daemon-reload"])
        self._systemctl(["enable", "--now", UNIT_NAME])
        return "Enoch service started."

    def stop(self, root: Path | None = None, *, allow_missing: bool = False) -> str:
        del root
        self._require_host()
        self._systemctl(["stop", UNIT_NAME], allow_failure=allow_missing)
        return "Enoch service stopped."

    def restart(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._write_manifest(paths)
        self._systemctl(["daemon-reload"])
        self._systemctl(["restart", UNIT_NAME])
        return "Enoch service restarted."

    def status(self, root: Path | None = None) -> str:
        del root
        self._require_host()
        result = self._systemctl(["is-active", "--quiet", UNIT_NAME], allow_failure=True)
        return "Enoch service is running." if result.returncode == 0 else "Enoch service is not running."

    def logs(self, root: Path | None = None, *, lines: int = 80) -> str:
        del root
        self._require_host()
        result = subprocess.run(
            ["journalctl", "--user", "-u", UNIT_NAME, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ServiceProviderError(detail or "journalctl failed.")
        return result.stdout.rstrip() or "No service logs yet."

    def doctor(self, root: Path | None = None) -> str:
        paths = self.paths(root)
        systemctl = shutil.which("systemctl")
        active = False
        if supports_host() and systemctl:
            active = self._systemctl(
                ["is-active", "--quiet", UNIT_NAME],
                allow_failure=True,
            ).returncode == 0
        return "\n".join(
            [
                "Service provider: systemd",
                _check("host", supports_host(), platform.system()),
                _check("systemctl", bool(systemctl), systemctl or "not found"),
                _check("manifest", paths.unit.exists(), str(paths.unit)),
                _check("service", active, UNIT_NAME),
            ]
        )

    def manifest(self, root: Path | None = None) -> str:
        return unit_text(self.paths(root))

    def schedule_restart(self, root: Path | None = None) -> None:
        del root
        self._schedule("restart")

    def schedule_stop(self, root: Path | None = None) -> None:
        del root
        self._schedule("stop")

    def paths(self, root: Path | None = None) -> SystemdPaths:
        resolved_root = Path(root or Path.cwd()).resolve()
        home = self.home or Path.home()
        directory = home / ".config" / "systemd" / "user"
        return SystemdPaths(
            root=resolved_root,
            unit_directory=directory,
            unit=directory / UNIT_NAME,
        )

    def _write_manifest(self, paths: SystemdPaths) -> None:
        paths.unit_directory.mkdir(parents=True, exist_ok=True)
        paths.unit.write_text(unit_text(paths), encoding="utf-8")

    def _systemctl(
        self,
        args: list[str],
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and not allow_failure:
            detail = (result.stderr or result.stdout).strip()
            raise ServiceProviderError(detail or f"systemctl failed: {' '.join(args)}")
        return result

    def _require_host(self) -> None:
        if not supports_host():
            raise ServiceProviderError("systemd provider is available only on Linux.")
        if shutil.which("systemctl") is None:
            raise ServiceProviderError("systemctl is not available on PATH.")

    def _schedule(self, action: str) -> None:
        subprocess.Popen(
            ["systemctl", "--user", action, UNIT_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def unit_text(paths: SystemdPaths) -> str:
    root = _quote(str(paths.root))
    executable = _quote(str(paths.root / "bin" / "enoch-agent"))
    python = _quote(os.environ.get("ENOCH_PYTHON", sys.executable))
    python_path = _quote(str(paths.root / "src"))
    path = _quote(os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    return "\n".join(
        [
            "[Unit]",
            "Description=Enoch agent",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f'WorkingDirectory="{root}"',
            f'ExecStart="{executable}"',
            "Restart=always",
            "RestartSec=5",
            f'Environment="PATH={path}"',
            f'Environment="PYTHONPATH={python_path}"',
            f'Environment="ENOCH_PYTHON={python}"',
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _check(name: str, passed: bool, detail: str) -> str:
    status = "ok" if passed else "needs attention"
    return f"- {name}: {status} ({detail})"


def create_provider(_root: Path | None = None) -> SystemdServiceProvider:
    return SystemdServiceProvider()


ENOCH_PROVIDERS = (
    {
        "kind": "service",
        "name": "systemd",
        "factory": create_provider,
        "supports": supports_host,
        "default": True,
    },
)


__all__ = [
    "ENOCH_PROVIDERS",
    "SystemdPaths",
    "SystemdServiceProvider",
    "UNIT_NAME",
    "create_provider",
    "supports_host",
    "unit_text",
]
