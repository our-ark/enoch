from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

from enoch.brain import REASONING_EFFORTS, codex_model_options, model_summary
from enoch.command_surface import lineage_usage
from enoch.config import write_section_value
from enoch.identity import Identity, identity_file_path, load_identity, update_mission
from enoch.identity_context import display_ancestor
from enoch.immune import ImmuneResult, run_immune_system
from enoch.learn import learn_command
from enoch.lineage.core import (
    LineageError,
    LineageInboxReport,
    LineageResolution,
    find_parent_inbox_candidate,
    format_candidate,
    format_lineage,
    format_parent_inherit_report,
    load_current_agent_profile,
    load_inbox_candidates,
    mark_inbox_candidate,
    refresh_lineage_inbox,
    resolve_lineage,
)
from enoch.skills import skills_command
from enoch.task_config import (
    format_task_timeout,
    parse_task_timeout,
    save_task_timeout,
    task_settings,
)


_CODEX_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


ModelSummaryFn = Callable[[Path], str]
DoctorFn = Callable[[Path], ImmuneResult]
ResolveLineageFn = Callable[[Path], LineageResolution]
RefreshLineageFn = Callable[..., LineageInboxReport]
FormatDoctorFn = Callable[[ImmuneResult], str]


def status_message(
    identity: Identity,
    root: Path,
    *,
    allowed_chat_id: int | None,
    chat_id: int | None = None,
    model_summary_fn: ModelSummaryFn = model_summary,
) -> str:
    lines = [
        f"{identity.name} status:",
        "",
        model_summary_fn(root),
        "",
        "Local state:",
    ]
    if chat_id is not None:
        lines.append(f"- Telegram chat id: {chat_id}")
    lines.extend(
        [
            f"- Telegram chat lock: {allowed_chat_id if allowed_chat_id is not None else 'not set'}",
        ]
    )
    if allowed_chat_id is None and chat_id is not None:
        lines.extend(
            [
                "",
                "Lock Enoch to this Telegram chat with:",
                f"bin/enoch setup-chat {chat_id}",
                "Then restart Enoch:",
                "bin/enoch-daemon restart",
            ]
        )
    return "\n".join(lines)


def identity_summary(identity: Identity, root: Path | None = None) -> str:
    ancestor = display_ancestor(identity, root)
    return "\n".join(
        [
            f"I am {identity.name}.",
            f"Role: {identity.role}",
            f"Generation: {identity.generation}",
            f"Ancestor: {ancestor}",
            f"Mission: {identity.mission}",
        ]
    )


def mission_command(text: str, identity: Identity, root: Path, *, prefix: str = "/") -> str:
    parts = text.split(maxsplit=1)
    command = f"{prefix}mission"
    if len(parts) == 1:
        current = _current_identity(identity, root)
        return "\n".join(
            [
                f"{current.name} mission:",
                current.mission,
                "",
                f"Update with {command} <new mission>.",
            ]
        )
    try:
        mission = update_mission(parts[1], root)
    except (OSError, ValueError) as error:
        return f"Enoch could not update her mission: {error}"
    return f"Enoch mission updated.\nMission: {mission}"


def _current_identity(identity: Identity, root: Path) -> Identity:
    try:
        return load_identity(identity_file_path(root))
    except (OSError, ValueError, KeyError):
        return identity


def thinking_command(
    text: str,
    root: Path,
    *,
    allowed_chat_id: int | None,
    model_summary_fn: ModelSummaryFn = model_summary,
    write_config: Callable[[str, str, str | None, Path | None], Path] = write_section_value,
    prefix: str = "/",
) -> str:
    parts = text.split()
    if len(parts) == 1:
        return thinking_status(root, model_summary_fn=model_summary_fn, prefix=prefix)
    choice = parts[1].strip().lower()
    if choice in {"default", "reset", "off"}:
        if allowed_chat_id is None:
            return thinking_lock_message()
        write_config("codex", "reasoning_effort", None, root)
        return "\n".join(
            [
                "Enoch cleared her local thinking override.",
                "",
                thinking_status(root, model_summary_fn=model_summary_fn, prefix=prefix),
            ]
        )
    if choice not in REASONING_EFFORTS:
        return thinking_usage(prefix=prefix)
    if allowed_chat_id is None:
        return thinking_lock_message()
    write_config("codex", "reasoning_effort", choice, root)
    return "\n".join(
        [
            f"Enoch thinking level set to {choice}.",
            "",
            thinking_status(root, model_summary_fn=model_summary_fn, prefix=prefix),
        ]
    )


def thinking_status(
    root: Path,
    *,
    model_summary_fn: ModelSummaryFn = model_summary,
    prefix: str = "/",
) -> str:
    command = f"{prefix}thinking"
    return "\n".join(
        [
            "Enoch thinking status:",
            model_summary_fn(root),
            "",
            f"Set with {command} low, {command} medium, {command} high, or {command} default.",
        ]
    )


def thinking_usage(prefix: str = "/") -> str:
    command = f"{prefix}thinking"
    return "\n".join(
        [
            f"Use {command} low, {command} medium, {command} high, or {command} default.",
            f"Use {command} by itself to show the current setting.",
        ]
    )


def thinking_lock_message() -> str:
    return "Enoch needs Telegram to be locked to one chat before changing her thinking level."


def config_command(text: str, root: Path, *, prefix: str = "/") -> str:
    parts = text.split()
    if len(parts) == 1:
        return config_status(root, prefix=prefix)
    setting = parts[1].lower().replace("_", "-")
    if setting == "task-timeout":
        if len(parts) == 2:
            return config_status(root, prefix=prefix)
        if len(parts) != 3:
            return config_usage(prefix=prefix)
        value = parts[2].lower()
        if value in {"default", "reset"}:
            settings = save_task_timeout(None, root)
        else:
            try:
                timeout = parse_task_timeout(value)
            except ValueError as error:
                return str(error)
            settings = save_task_timeout(timeout, root)
        return "\n".join(
            [
                f"Task timeout set to {format_task_timeout(settings.timeout_seconds)}"
                + (" (default)." if settings.uses_default_timeout else "."),
                "",
                config_status(root, prefix=prefix),
            ]
        )
    if setting == "model":
        if len(parts) == 2:
            return model_config_status(root, prefix=prefix)
        if len(parts) != 3:
            return config_usage(prefix=prefix)
        value = parts[2].strip()
        if value.lower() in {"default", "reset"}:
            write_section_value("codex", "model", None, root)
            message = "Enoch cleared her local Codex model override."
        elif not _CODEX_MODEL_PATTERN.fullmatch(value):
            return "Codex model must be one model identifier without spaces."
        else:
            write_section_value("codex", "model", value, root)
            message = f"Enoch Codex model set to {value}."
        return "\n\n".join([message, model_config_status(root, prefix=prefix)])
    if setting == "reasoning-effort":
        if len(parts) == 2:
            return config_status(root, prefix=prefix)
        if len(parts) != 3:
            return config_usage(prefix=prefix)
        value = parts[2].strip().lower()
        if value in {"default", "reset"}:
            write_section_value("codex", "reasoning_effort", None, root)
            message = "Enoch cleared her local Codex reasoning effort override."
        elif value not in REASONING_EFFORTS:
            return "Codex reasoning effort must be low, medium, high, or default."
        else:
            write_section_value("codex", "reasoning_effort", value, root)
            message = f"Enoch Codex reasoning effort set to {value}."
        return "\n\n".join([message, config_status(root, prefix=prefix)])
    return config_usage(prefix=prefix)


def config_status(root: Path, *, prefix: str = "/") -> str:
    settings = task_settings(root)
    default = " (default)" if settings.uses_default_timeout else ""
    command = f"{prefix}config"
    return "\n".join(
        [
            "Enoch config:",
            f"- Task timeout: {format_task_timeout(settings.timeout_seconds)}{default}",
            "",
            "Codex:",
            model_summary(root),
            "",
            f"Use {command} model to see available models or set one with {command} model <name>.",
            (
                f"Set reasoning with {command} reasoning-effort low|medium|high "
                f"or {command} reasoning-effort default."
            ),
            f"Set task timeout with {command} task-timeout <duration> or {command} task-timeout default.",
        ]
    )


def model_config_status(root: Path, *, prefix: str = "/") -> str:
    command = f"{prefix}config"
    summary = model_summary(root)
    current = _model_name_from_summary(summary)
    lines = ["Codex model:", summary, "", "Available GPT-5.6 models:"]
    options = tuple(
        option
        for option in codex_model_options()
        if option.slug == "gpt-5.6" or option.slug.startswith("gpt-5.6-")
    )
    if options:
        for option in options:
            current_label = " [current]" if option.slug == current else ""
            description = f" - {option.description}" if option.description else ""
            lines.append(f"- {option.slug}{current_label}{description}")
    else:
        lines.append("- unavailable; Enoch could not find GPT-5.6 models in the installed Codex catalog")
    lines.extend(
        [
            "",
            f"Example: {command} model gpt-5.6-sol",
            f"Use {command} model default to inherit the Codex default.",
            "Other valid model ids are accepted for private or future Codex rollouts.",
        ]
    )
    return "\n".join(lines)


def _model_name_from_summary(summary: str) -> str:
    prefix = "AI model: "
    return next(
        (line[len(prefix) :].strip() for line in summary.splitlines() if line.startswith(prefix)),
        "",
    )


def config_usage(prefix: str = "/") -> str:
    command = f"{prefix}config"
    return "\n".join(
        [
            "Config commands:",
            f"{command} - show local system settings",
            f"{command} model - show the effective and available Codex models",
            f"{command} model <name> - set a local Codex model override",
            f"{command} model default - inherit the Codex model",
            f"{command} reasoning-effort - show the effective reasoning effort",
            f"{command} reasoning-effort low|medium|high - set local reasoning effort",
            f"{command} reasoning-effort default - inherit Codex reasoning effort",
            f"{command} task-timeout - show the task timeout",
            f"{command} task-timeout <duration> - set a timeout between 1m and 2h",
            f"{command} task-timeout default - restore the 10m default",
        ]
    )


def lineage_command(
    text: str,
    root: Path,
    *,
    prefix: str = "/",
    command_name: str = "ancestors",
    resolve_lineage_fn: ResolveLineageFn = resolve_lineage,
) -> str:
    parts = text.split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) >= 2 else ""
    try:
        if not subcommand:
            resolution = resolve_lineage_fn(root)
            candidates = load_inbox_candidates(root)
            return "\n\n".join(
                [
                    format_lineage(
                        resolution.ancestors,
                        resolution.warnings,
                        candidates,
                        load_current_agent_profile(root),
                    ),
                    lineage_usage(prefix, command_name=command_name),
                ]
            )
    except LineageError as error:
        return f"Enoch could not complete lineage command: {error}"
    return lineage_usage(prefix, command_name=command_name)


def inherit_command(
    text: str,
    root: Path,
    *,
    prefix: str = "/",
    command_name: str = "inherit",
    refresh_lineage_fn: RefreshLineageFn = refresh_lineage_inbox,
) -> str:
    parts = text.split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) >= 2 else ""
    argument = parts[2].strip() if len(parts) >= 3 else ""
    try:
        if not subcommand or subcommand in {"show", "changes", "inbox", "refresh"}:
            report = refresh_lineage_fn(root, scope="parent")
            return format_parent_inherit_report(report)
        if subcommand == "inspect":
            if not argument:
                return f"Use {prefix}{command_name} inspect <candidate>."
            candidate = find_parent_inbox_candidate(argument, root)
            if candidate is None:
                return (
                    f"Enoch could not find direct-parent change {argument}. "
                    f"Run {prefix}{command_name} first."
                )
            return format_candidate(candidate)
        if subcommand == "ignore":
            if not argument:
                return f"Use {prefix}{command_name} ignore <candidate>."
            candidate = mark_inbox_candidate(argument, "ignored", root, note="Ignored by user command.")
            return f"Ignored inheritable change {candidate.id}."
    except LineageError as error:
        return f"Enoch could not complete inherit command: {error}"
    return lineage_usage(prefix, command_name=command_name)


def doctor_command(
    root: Path,
    *,
    format_doctor: FormatDoctorFn,
    run_doctor: DoctorFn = run_immune_system,
) -> str:
    return format_doctor(run_doctor(root))


def help_message(topic: str = "") -> str:
    normalized_topic = _normalize_help_topic(topic)
    if normalized_topic:
        topic_message = _help_topic_message(normalized_topic)
        if topic_message:
            return topic_message
        return f"No help found for /{normalized_topic}.\nUse /help to see available commands."
    return "\n".join(
        [
            "Enoch Telegram commands:",
            "/help - show this command list",
            "",
            "Common:",
            "/self - show Enoch's identity, role, ancestor, and mission",
            "/mission [text] - show or update Enoch's mission",
            "/status - show identity, model, local state, and chat setup",
            "",
            "Work:",
            "/do <request> - run work now instead of queueing it",
            "/task <request> - queue background work for Enoch",
            "/tasks - show running, queued, and recent task history",
            "/stop - stop the currently running task",
            "/backlog [p0|p1|p2] <request> - save deferred work for idle time",
            "/cron - show scheduled jobs",
            "",
            "Inherit:",
            "/ancestors - show ancestor chain and ancestor skills",
            "/inherit - show inheritable direct-parent changes",
            "",
            "Learn:",
            "/skills [agent-or-path] - show declared skills",
            "/learn <skill> from <agent> - adapt a published skill from another agent",
            "",
            "Evolve:",
            "/feedback - show feedback signals available to self-evolution",
            "/experience - show task provenance statistics and evolution candidates",
            "/propose - rank all evolve sources and propose the strongest candidate",
            "/evolve - show self-evolution mode, theme, and top candidate",
            "",
            "System:",
            "/config - show or update local system settings",
            "/resume - continue tasks paused while Codex access was unavailable",
            "/doctor - run local health checks",
            "/update - pull latest main, run doctor, and restart if safe",
            "/restart - restart Enoch's Telegram daemon from the locked chat",
            "",
            "For repository changes, say the request naturally. Enoch will open a PR automatically when Codex requests an edit.",
        ]
    )


def _normalize_help_topic(topic: str) -> str:
    stripped = topic.strip().lower()
    if not stripped:
        return ""
    first = stripped.split(maxsplit=1)[0]
    return first.lstrip("/")


def _help_topic_message(topic: str) -> str:
    if topic == "help":
        return "\n".join(
            [
                "Help commands:",
                "/help - show all commands",
                "/help <command> - show usage for one command",
            ]
        )
    if topic == "ancestors":
        return lineage_usage("/", command_name="ancestors")
    if topic == "inherit":
        return lineage_usage("/", command_name="inherit")
    if topic in {"backlog", "backlogs"}:
        return "\n".join(
            [
                "Backlog commands:",
                "/backlog [p0|p1|p2] <request> - save deferred work",
                "/backlog remove <id> - remove a pending backlog item",
                "/backlog priority <id> p0|p1|p2 - reprioritize a pending backlog item",
                "/backlog promote <id> - move a pending backlog item into the active task queue",
            ]
        )
    if topic in {"cron", "crons"}:
        return "\n".join(
            [
                "Cron commands:",
                "/cron every <interval> <request> - schedule recurring work",
                "Intervals can be like 10m, 2h, or 1d.",
                "/cron cancel <id> - cancel a scheduled job",
                "/cron - show scheduled jobs",
            ]
        )
    if topic == "evolve":
        return "\n".join(
            [
                "Evolve commands:",
                "/evolve - show self-evolution mode, theme, and top candidate",
                "/evolve mode <mode> - set self-evolution behavior",
                "Modes: disabled, co-evolve, auto-evolve.",
                "/evolve theme [text] - show or set the current self-evolution theme",
                "/evolve brainstorm - generate bounded candidates under the current theme",
                "/evolve list - show current self-evolution candidates",
                "/evolve approve <id> - approve and queue a self-evolution candidate",
                "/evolve retry <id> - retry a failed self-evolution candidate as a new task",
                "/evolve remove <id> - remove a self-evolution candidate",
                "/evolve schedule <text> - let Enoch interpret common schedule text",
            ]
        )
    if topic == "config":
        return config_usage("/")
    topics = {
        "start": "/start - start Enoch and point to /help",
        "self": "/self - show Enoch's identity, role, ancestor, and mission",
        "status": "/status - show identity, model, local state, and chat setup",
        "mission": "/mission [text] - show Enoch's mission or update it with new text",
        "do": "/do <request> - run work now instead of queueing it",
        "task": "\n".join(
            [
                "Task commands:",
                "/task <request> - queue background work for Enoch",
                "/task cancel <id> - cancel a queued background task",
            ]
        ),
        "tasks": "/tasks - show running, queued, and recent task history",
        "stop": "/stop - stop the currently running /do or /task job",
        "feedback": "/feedback - show feedback signals available to self-evolution",
        "experience": "/experience - show task provenance statistics and evolution candidates",
        "propose": "/propose - rank all evolve sources and propose the strongest candidate",
        "skills": "/skills [agent-or-path] - show declared skills",
        "learn": "/learn <skill> from <agent> - adapt a published skill from another Our-Ark agent",
        "doctor": "/doctor - run local health checks",
        "resume": "/resume - continue tasks paused while Codex access was unavailable",
        "update": "/update - pull latest main, run doctor, and restart if safe",
        "restart": "/restart - restart Enoch's Telegram daemon from the locked chat",
        "thinking": thinking_usage("/"),
    }
    return topics.get(topic, "")


def action_lock_message() -> str:
    return "\n".join(
        [
            "Enoch will not change code or coordinate GitHub unless Telegram is locked to one chat.",
            "Run `bin/enoch setup-chat <chat_id>` and restart Enoch.",
        ]
    )
