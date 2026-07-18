from __future__ import annotations

import sys
from pathlib import Path

from enoch.brain import model_summary
from enoch.formatting import format_doctor_result, summarize_for_log
from enoch.identity import Identity, load_identity
from enoch.identity_context import display_ancestor
from enoch.immune import run_immune_system
from enoch.instance import InstanceError, format_instance_init_result, init_instance
from enoch.logs import log_system_event
from enoch.memory.store import ensure_long_term_memory
from enoch.providers.registry import load_provider
from enoch.commands import (
    config_command,
    inherit_command,
    learn_command,
    lineage_command,
    mission_command,
    skills_command,
    thinking_command,
)
from enoch.setup_tools import setup_command
from enoch.update_tools import schedule_daemon_restart as _schedule_daemon_restart
from enoch.updater import update_from_main


HELP = """Commands:
  help        Show this help.
  init        Create or claim a local Enoch instance worktree.
  status      Show identity, model, and lineage.
  setup       Configure Telegram token, chat lock, and setup status.
  config      Show settings or select installed providers.
  thinking    Show or set Enoch's Codex thinking level.
  mission     Show or update Enoch's mission.
  ancestors   Inspect ancestor chain and inheritable updates.
  inherit     Show inheritable direct-parent changes.
  skills      Show declared skills for Enoch or another local agent.
  learn       Inspect a published skill for adaptation.
  doctor      Run Enoch's local health checks.
  update      Pull latest main, run doctor, and restart Enoch if safe.
  exit        Put Enoch back to sleep.

Enoch CLI is admin-only. Use Telegram for conversation, repository edits, and self-evolution.
"""

ADMIN_ONLY_MESSAGE = "\n".join(
    [
        "Enoch CLI is admin-only now.",
        "Use Telegram for conversation, repository edits, and self-evolution.",
        "Type `help` to see available CLI commands.",
    ]
)

EXIT = object()


def main(argv: list[str] | None = None) -> None:
    identity = load_identity()
    root = Path.cwd()
    args = sys.argv[1:] if argv is None else argv
    if args:
        result = _command_output(" ".join(args), identity, root)
        if result is EXIT:
            return
        if result:
            print(result)
        return
    _print_wake(identity)
    _repl(identity, root)


def _print_wake(identity: Identity) -> None:
    print("Enoch is awake.")
    print()
    print(f"Name: {identity.name}")
    print(f"Role: {_humanize(identity.role)}")
    print(f"Generation: {identity.generation}")
    print(f"Origin: {identity.origin.ark} / {identity.origin.created_by}")
    print(f"Mission: {identity.mission}")
    print()
    print("Type 'help' for admin commands.")


def _repl(identity: Identity, root: Path) -> None:
    while True:
        try:
            raw_command = input("\nenoch> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nEnoch is asleep.")
            return

        command = raw_command.lower()
        if command == "":
            continue
        result = _command_output(raw_command, identity, root)
        if result is EXIT:
            print("Enoch is asleep.")
            return
        if result:
            print(result)


def _command_output(raw_command: str, identity: Identity, root: Path) -> str | object:
    command = raw_command.strip().lower()
    if command == "":
        return ""
    if command == "exit":
        return EXIT
    if command == "help":
        return HELP
    if command == "init" or command.startswith("init "):
        return _init_instance(identity, raw_command, root)
    if command == "status":
        return _status_text(identity, root)
    if command == "setup" or command.startswith("setup "):
        return _setup(raw_command, root)
    if command == "setup-chat" or command.startswith("setup-chat "):
        return _setup(raw_command, root)
    if command == "setup-token" or command.startswith("setup-token "):
        return _setup(raw_command, root)
    if command == "config" or command.startswith("config "):
        return config_command(raw_command, root, prefix="")
    if command == "thinking" or command.startswith("thinking "):
        return _thinking(raw_command, root)
    if command == "mission" or command.startswith("mission "):
        return _mission(raw_command, identity, root)
    if command == "ancestors" or command.startswith("ancestors "):
        return _ancestors(raw_command, root)
    if command == "inherit" or command.startswith("inherit "):
        return _inherit(raw_command, root)
    if command == "skills" or command.startswith("skills "):
        return _skills(raw_command, root)
    if command == "learn" or command.startswith("learn "):
        return _learn(raw_command, root)
    if command == "doctor":
        return _doctor_text(root)
    if command == "update":
        return _update(root)
    return ADMIN_ONLY_MESSAGE


def _status(identity: Identity, root: Path) -> None:
    print(_status_text(identity, root))


def _status_text(identity: Identity, root: Path) -> str:
    ancestor = display_ancestor(identity, root)
    runtime = load_provider("runtime", root)
    summary = model_summary(root) if runtime.name == "codex" else runtime.model_summary(root)
    return "\n".join(
        [
            f"{identity.name} status:",
            "",
            f"I am {identity.name}.",
            f"Role: {identity.role}",
            f"Generation: {identity.generation}",
            f"Ancestor: {ancestor}",
            f"Mission: {identity.mission}",
            "",
            summary,
        ]
    )


def _setup(text: str, root: Path) -> str:
    return setup_command(text, root, prefix="")


def _init_instance(identity: Identity, text: str, root: Path) -> str:
    try:
        options = _parse_init_options(text)
        result = init_instance(
            identity,
            root,
            instance_name=options["instance"],
            worktree=Path(options["worktree"]) if options["worktree"] else None,
            branch=options["branch"],
        )
    except (InstanceError, ValueError) as error:
        return f"Enoch could not initialize that instance: {error}"
    return format_instance_init_result(result)


def _parse_init_options(text: str) -> dict[str, str]:
    parts = text.split()
    options = {"instance": "default", "worktree": "", "branch": ""}
    index = 1
    while index < len(parts):
        option = parts[index]
        if option in {"--instance", "--worktree", "--branch"}:
            if index + 1 >= len(parts):
                raise ValueError(f"{option} requires a value.")
            options[option.removeprefix("--")] = parts[index + 1]
            index += 2
            continue
        if not option.startswith("--") and options["instance"] == "default":
            options["instance"] = option
            index += 1
            continue
        raise ValueError("Use init [--instance name] [--worktree path] [--branch branch].")
    return options


def _mission(text: str, identity: Identity, root: Path) -> str:
    return mission_command(text, identity, root, prefix="")


def _thinking(text: str, root: Path) -> str:
    runtime = load_provider("runtime", root)
    summary_fn = model_summary if runtime.name == "codex" else runtime.model_summary
    return thinking_command(
        text,
        root,
        allowed_chat_id=0,
        model_summary_fn=summary_fn,
        prefix="",
        runtime=runtime,
    )


def _skills(text: str, root: Path) -> str:
    return skills_command(text, root, prefix="")


def _learn(text: str, root: Path) -> str:
    return learn_command(text, root, prefix="")


def _update(root: Path) -> str:
    result = update_from_main(root)
    if result.direct_action_result:
        _record_direct_action("update from main", result.direct_action_result, root)
    if result.restart_required:
        _schedule_daemon_restart(root)
    return result.message


def _ancestors(text: str, root: Path) -> str:
    return lineage_command(text, root, prefix="", command_name="ancestors")


def _inherit(text: str, root: Path) -> str:
    return inherit_command(text, root, prefix="", command_name="inherit")


def _doctor(root: Path) -> None:
    print(_doctor_text(root))


def _doctor_text(root: Path) -> str:
    return format_doctor_result(run_immune_system(root))


def _record_direct_action(message: str, result: str, root: Path) -> None:
    try:
        log_system_event(
            "direct_action",
            root=root,
            details={
                "request": _summarize_for_log(message),
                "result": _summarize_for_log(result),
            },
        )
    except OSError:
        return
    try:
        ensure_long_term_memory(root)
    except OSError:
        return


def _summarize_for_log(text: str, limit: int = 2000) -> str:
    return summarize_for_log(text, limit)


def _humanize(value: str) -> str:
    return value.replace("_", " ").title()


if __name__ == "__main__":
    sys.exit(main())
