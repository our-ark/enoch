from __future__ import annotations

import argparse
from pathlib import Path
import sys

from enoch.config import config_path
from enoch.providers.contracts import ChatProviderError, ServiceProvider, ServiceProviderError
from enoch.providers.registry import ProviderError, load_provider, provider_name
from enoch.runtime import DEFAULT_DAEMON_LOG_LINES


class DaemonError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage Enoch as a background service.")
    parser.add_argument(
        "command",
        choices=[
            "install",
            "uninstall",
            "start",
            "stop",
            "restart",
            "status",
            "logs",
            "doctor",
            "manifest",
        ],
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=DEFAULT_DAEMON_LOG_LINES,
        help="Number of log lines to show.",
    )
    args = parser.parse_args(argv)

    try:
        message = dispatch(args.command, lines=args.lines)
    except (DaemonError, ProviderError, ServiceProviderError) as error:
        print(str(error))
        raise SystemExit(1) from error
    if message:
        print(message)


def dispatch(
    command: str,
    lines: int = DEFAULT_DAEMON_LOG_LINES,
    root: Path | None = None,
) -> str:
    if command == "install":
        return install(root)
    if command == "uninstall":
        return _service(root).uninstall(root)
    if command == "start":
        return start(root)
    if command == "stop":
        return _service(root).stop(root)
    if command == "restart":
        return restart(root)
    if command == "status":
        return _service(root).status(root)
    if command == "logs":
        return _service(root).logs(root, lines=lines)
    if command == "doctor":
        return doctor(root)
    if command == "manifest":
        return _service(root).manifest(root)
    raise DaemonError(f"Unknown daemon command: {command}")


def install(root: Path | None = None) -> str:
    resolved = _root(root)
    _require_daemon_config(resolved)
    return _service(resolved).install(resolved)


def start(root: Path | None = None) -> str:
    resolved = _root(root)
    _require_daemon_config(resolved)
    return _service(resolved).start(resolved)


def restart(root: Path | None = None) -> str:
    resolved = _root(root)
    _require_daemon_config(resolved)
    return _service(resolved).restart(resolved)


def doctor(root: Path | None = None) -> str:
    resolved = _root(root)
    try:
        chat = provider_name("chat", resolved)
    except ProviderError:
        chat = "not configured"
    config_ready = _has_daemon_config(resolved)
    service = _service(resolved)
    return "\n".join(
        [
            "Enoch service doctor:",
            _check(
                "config",
                config_ready,
                f"{chat} provider in {config_path(resolved)}",
            ),
            service.doctor(resolved),
        ]
    )


def _service(root: Path | None = None) -> ServiceProvider:
    provider = load_provider("service", root)
    return provider


def _root(root: Path | None) -> Path:
    return Path(root or Path.cwd()).resolve()


def _require_daemon_config(root: Path) -> None:
    if not _has_daemon_config(root):
        raise DaemonError(
            "Configure the selected chat provider before starting Enoch service. "
            f"Local config path: {config_path(root)}."
        )


def _has_daemon_config(root: Path) -> bool:
    try:
        selected = provider_name("chat", root)
        load_provider("chat", root, name=selected)
    except (ProviderError, ChatProviderError):
        return False
    return True


def _check(name: str, passed: bool, detail: str) -> str:
    status = "ok" if passed else "needs attention"
    return f"- {name}: {status} ({detail})"


if __name__ == "__main__":
    main(sys.argv[1:])
