from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import platform
import subprocess
import sys

from enoch.config import config_path, read_section
from enoch.logs import daemon_log_dir
from enoch.paths import enoch_home, repo_root
from enoch.runtime import DEFAULT_DAEMON_LOG_LINES


LABEL = "com.ourark.enoch"
PLIST_NAME = f"{LABEL}.plist"


class DaemonError(RuntimeError):
    pass


@dataclass(frozen=True)
class DaemonPaths:
    root: Path
    home: Path
    launch_agents: Path
    plist: Path
    logs: Path
    stdout: Path
    stderr: Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage Enoch as a background daemon.")
    parser.add_argument(
        "command",
        choices=["install", "uninstall", "start", "stop", "restart", "status", "logs", "doctor", "plist"],
    )
    parser.add_argument("--lines", type=int, default=DEFAULT_DAEMON_LOG_LINES, help="Number of log lines to show.")
    args = parser.parse_args(argv)

    try:
        message = dispatch(args.command, lines=args.lines)
    except DaemonError as error:
        print(str(error))
        raise SystemExit(1) from error
    if message:
        print(message)


def dispatch(command: str, lines: int = DEFAULT_DAEMON_LOG_LINES, root: Path | None = None) -> str:
    if command == "install":
        return install(root=root)
    if command == "uninstall":
        return uninstall(root=root)
    if command == "start":
        return start(root=root)
    if command == "stop":
        return stop(root=root)
    if command == "restart":
        stop(root=root, allow_missing=True)
        return start(root=root)
    if command == "status":
        return status()
    if command == "logs":
        return logs(lines=lines, root=root)
    if command == "doctor":
        return doctor(root=root)
    if command == "plist":
        paths = daemon_paths(root=root)
        return plist_text(paths)
    raise DaemonError(f"Unknown daemon command: {command}")


def install(root: Path | None = None) -> str:
    _require_macos()
    paths = daemon_paths(root=root)
    _require_daemon_config(paths.root)
    _write_plist(paths)
    _launchctl(["bootstrap", _domain(), str(paths.plist)], allow_failure=True)
    return f"Enoch daemon installed at {paths.plist}."


def uninstall(root: Path | None = None) -> str:
    _require_macos()
    paths = daemon_paths(root=root)
    _launchctl(["bootout", _domain(), str(paths.plist)], allow_failure=True)
    if paths.plist.exists():
        paths.plist.unlink()
    return "Enoch daemon uninstalled."


def start(root: Path | None = None) -> str:
    _require_macos()
    paths = daemon_paths(root=root)
    _require_daemon_config(paths.root)
    _write_plist(paths)
    _launchctl(["bootstrap", _domain(), str(paths.plist)], allow_failure=True)
    _launchctl(["kickstart", "-k", f"{_domain()}/{LABEL}"])
    return "Enoch daemon started."


def stop(root: Path | None = None, allow_missing: bool = False) -> str:
    _require_macos()
    paths = daemon_paths(root=root)
    _launchctl(["bootout", _domain(), str(paths.plist)], allow_failure=allow_missing)
    return "Enoch daemon stopped."


def status() -> str:
    if _daemon_loaded():
        return "Enoch daemon is loaded."
    return "Enoch daemon is not loaded."


def logs(lines: int = DEFAULT_DAEMON_LOG_LINES, root: Path | None = None) -> str:
    paths = daemon_paths(root=root)
    sections = [
        _tail_section("stdout", paths.stdout, lines),
        _tail_section("stderr", paths.stderr, lines),
    ]
    return "\n\n".join(sections)


def doctor(root: Path | None = None) -> str:
    paths = daemon_paths(root=root)
    checks = [
        _check("config", _has_daemon_config(paths.root), f"Telegram bot token in {config_path(paths.root)}"),
        _check("launch agent", paths.plist.exists(), str(paths.plist)),
        _check("log directory", paths.logs.exists(), str(paths.logs)),
    ]
    try:
        loaded = _daemon_loaded()
    except DaemonError:
        loaded = False
    checks.append(_check("launchd", loaded, LABEL))
    return "\n".join(["Enoch daemon plumbing doctor:", *checks])


def daemon_paths(root: Path | None = None, home: Path | None = None) -> DaemonPaths:
    resolved_root = repo_root(root)
    resolved_home = home or Path.home()
    launch_agents = resolved_home / "Library" / "LaunchAgents"
    daemon_home = enoch_home(resolved_root)
    log_dir = daemon_log_dir(resolved_root)
    return DaemonPaths(
        root=resolved_root,
        home=daemon_home,
        launch_agents=launch_agents,
        plist=launch_agents / PLIST_NAME,
        logs=log_dir,
        stdout=log_dir / "enoch-daemon.out.log",
        stderr=log_dir / "enoch-daemon.err.log",
    )


def _write_plist(paths: DaemonPaths) -> None:
    paths.launch_agents.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    paths.plist.write_bytes(plist_bytes(paths))


def plist_bytes(paths: DaemonPaths) -> bytes:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(paths.root / "bin" / "enoch-telegram")],
        "WorkingDirectory": str(paths.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(paths.stdout),
        "StandardErrorPath": str(paths.stderr),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(paths.root / "src"),
            "ENOCH_PYTHON": os.environ.get("ENOCH_PYTHON", sys.executable),
        },
    }
    return plistlib.dumps(payload, sort_keys=False)


def plist_text(paths: DaemonPaths) -> str:
    return plist_bytes(paths).decode("utf-8")


def _tail_section(name: str, path: Path, lines: int) -> str:
    if not path.exists():
        return f"{name}: {path}\n(no log file yet)"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = "\n".join(content[-lines:])
    return f"{name}: {path}\n{tail}"


def _launchctl(args: list[str], allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if result.returncode != 0 and not allow_failure:
        detail = (result.stderr or result.stdout).strip()
        raise DaemonError(detail or f"launchctl failed: {' '.join(args)}")
    return result


def _daemon_loaded() -> bool:
    _require_macos()
    result = _launchctl(["print", f"{_domain()}/{LABEL}"], allow_failure=True)
    return result.returncode == 0


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise DaemonError("Enoch daemon currently uses macOS launchd.")


def _require_daemon_config(root: Path) -> None:
    if not _has_daemon_config(root):
        raise DaemonError(
            f"Run `bin/enoch setup-token <token>` before starting Enoch daemon. "
            f"Local config path: {config_path(root)}. "
            "launchd does not inherit terminal-only Telegram token environment variables."
        )


def _has_daemon_config(root: Path) -> bool:
    settings = read_section("telegram", root)
    return bool(settings.get("bot_token", "").strip())


def _check(name: str, passed: bool, detail: str) -> str:
    status_text = "ok" if passed else "needs attention"
    return f"- {name}: {status_text} ({detail})"


if __name__ == "__main__":
    main(sys.argv[1:])
