from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys

from our_ark_provider_kit import ServiceProviderError


LABEL = "com.ourark.enoch"
PLIST_NAME = f"{LABEL}.plist"


def supports_host() -> bool:
    return platform.system() == "Darwin"


@dataclass(frozen=True)
class LaunchdPaths:
    root: Path
    launch_agents: Path
    plist: Path
    logs: Path
    stdout: Path
    stderr: Path


class LaunchdServiceProvider:
    name = "launchd"
    provider_kind = "service"

    def __init__(self, *, home: Path | None = None) -> None:
        self.home = home

    def install(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._write_manifest(paths)
        self._launchctl(["bootstrap", self._domain(), str(paths.plist)], allow_failure=True)
        return f"Enoch service installed at {paths.plist}."

    def uninstall(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._launchctl(["bootout", self._domain(), str(paths.plist)], allow_failure=True)
        paths.plist.unlink(missing_ok=True)
        return "Enoch service uninstalled."

    def start(self, root: Path | None = None) -> str:
        self._require_host()
        paths = self.paths(root)
        self._write_manifest(paths)
        self._launchctl(["bootstrap", self._domain(), str(paths.plist)], allow_failure=True)
        self._launchctl(["kickstart", "-k", f"{self._domain()}/{LABEL}"])
        return "Enoch service started."

    def stop(self, root: Path | None = None, *, allow_missing: bool = False) -> str:
        self._require_host()
        paths = self.paths(root)
        self._launchctl(["bootout", self._domain(), str(paths.plist)], allow_failure=allow_missing)
        return "Enoch service stopped."

    def restart(self, root: Path | None = None) -> str:
        self.stop(root, allow_missing=True)
        return self.start(root)

    def status(self, root: Path | None = None) -> str:
        del root
        self._require_host()
        result = self._launchctl(["print", f"{self._domain()}/{LABEL}"], allow_failure=True)
        return "Enoch service is running." if result.returncode == 0 else "Enoch service is not running."

    def logs(self, root: Path | None = None, *, lines: int = 80) -> str:
        paths = self.paths(root)
        return "\n\n".join(
            [
                _tail_section("stdout", paths.stdout, lines),
                _tail_section("stderr", paths.stderr, lines),
            ]
        )

    def doctor(self, root: Path | None = None) -> str:
        paths = self.paths(root)
        loaded = False
        if supports_host():
            loaded = self._launchctl(
                ["print", f"{self._domain()}/{LABEL}"],
                allow_failure=True,
            ).returncode == 0
        return "\n".join(
            [
                "Service provider: launchd",
                _check("host", supports_host(), platform.system()),
                _check("manifest", paths.plist.exists(), str(paths.plist)),
                _check("log directory", paths.logs.exists(), str(paths.logs)),
                _check("service", loaded, LABEL),
            ]
        )

    def manifest(self, root: Path | None = None) -> str:
        return plist_bytes(self.paths(root)).decode("utf-8")

    def schedule_restart(self, root: Path | None = None) -> None:
        self._schedule(root, "restart")

    def schedule_stop(self, root: Path | None = None) -> None:
        self._schedule(root, "stop")

    def paths(self, root: Path | None = None) -> LaunchdPaths:
        resolved_root = Path(root or Path.cwd()).resolve()
        home = self.home or Path.home()
        launch_agents = home / "Library" / "LaunchAgents"
        logs = resolved_root / ".enoch" / "logs" / "daemon"
        return LaunchdPaths(
            root=resolved_root,
            launch_agents=launch_agents,
            plist=launch_agents / PLIST_NAME,
            logs=logs,
            stdout=logs / "enoch-daemon.out.log",
            stderr=logs / "enoch-daemon.err.log",
        )

    def _write_manifest(self, paths: LaunchdPaths) -> None:
        paths.launch_agents.mkdir(parents=True, exist_ok=True)
        paths.logs.mkdir(parents=True, exist_ok=True)
        paths.plist.write_bytes(plist_bytes(paths))

    def _launchctl(
        self,
        args: list[str],
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
        if result.returncode != 0 and not allow_failure:
            detail = (result.stderr or result.stdout).strip()
            raise ServiceProviderError(detail or f"launchctl failed: {' '.join(args)}")
        return result

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _require_host(self) -> None:
        if not supports_host():
            raise ServiceProviderError("launchd is available only on macOS.")

    def _schedule(self, root: Path | None, action: str) -> None:
        resolved_root = Path(root or Path.cwd()).resolve()
        subprocess.Popen(
            [str(resolved_root / "bin" / "enoch-daemon"), action],
            cwd=resolved_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def plist_bytes(paths: LaunchdPaths) -> bytes:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(paths.root / "bin" / "enoch-agent")],
        "WorkingDirectory": str(paths.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(paths.stdout),
        "StandardErrorPath": str(paths.stderr),
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH",
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ),
            "PYTHONPATH": str(paths.root / "src"),
            "ENOCH_PYTHON": os.environ.get("ENOCH_PYTHON", sys.executable),
        },
    }
    return plistlib.dumps(payload, sort_keys=False)


def _tail_section(name: str, path: Path, lines: int) -> str:
    if not path.exists():
        return f"{name}: {path}\n(no log file yet)"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return f"{name}: {path}\n" + "\n".join(content[-lines:])


def _check(name: str, passed: bool, detail: str) -> str:
    status = "ok" if passed else "needs attention"
    return f"- {name}: {status} ({detail})"


def create_provider(_root: Path | None = None) -> LaunchdServiceProvider:
    return LaunchdServiceProvider()


ENOCH_PROVIDERS = (
    {
        "kind": "service",
        "name": "launchd",
        "factory": create_provider,
        "supports": supports_host,
        "default": True,
    },
)


__all__ = [
    "ENOCH_PROVIDERS",
    "LABEL",
    "LaunchdPaths",
    "LaunchdServiceProvider",
    "create_provider",
    "plist_bytes",
    "supports_host",
]
