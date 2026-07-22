from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Callable

from enoch.brain import (
    REASONING_EFFORTS,
    codex_model_options,
    model_summary,
    resolve_codex_executable,
    resolve_codex_executable_value,
)
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
from enoch.providers.contracts import AgentRuntime
from enoch.providers.contracts import ConversationId
from enoch.providers.registry import (
    PROVIDER_KINDS,
    ProviderError,
    available_providers,
    provider_name,
)
from enoch.providers.runtime import FunctionAgentRuntime
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
    allowed_chat_id: ConversationId | None,
    chat_id: ConversationId | None = None,
    chat_provider: str = "chat",
    model_summary_fn: ModelSummaryFn = model_summary,
) -> str:
    provider_label = _provider_label(chat_provider)
    lines = [
        f"{identity.name} status:",
        "",
        model_summary_fn(root),
        "",
        "Local state:",
    ]
    if chat_id is not None:
        lines.append(f"- {provider_label} conversation id: {chat_id}")
    lines.extend(
        [
            f"- {provider_label} conversation lock: "
            f"{allowed_chat_id if allowed_chat_id is not None else 'not set'}",
        ]
    )
    if allowed_chat_id is None and chat_id is not None:
        lines.append("")
        lines.append(
            f"Configure the {provider_label} provider to lock Enoch to this conversation, then restart Enoch."
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
    allowed_chat_id: ConversationId | None,
    model_summary_fn: ModelSummaryFn = model_summary,
    write_config: Callable[[str, str, str | None, Path | None], Path] = write_section_value,
    prefix: str = "/",
    runtime: AgentRuntime | None = None,
) -> str:
    runtime = runtime or _default_runtime(root)
    if model_summary_fn is model_summary:
        model_summary_fn = runtime.model_summary
    parts = text.split()
    if len(parts) == 1:
        return thinking_status(root, model_summary_fn=model_summary_fn, prefix=prefix)
    choice = parts[1].strip().lower()
    if choice in {"default", "reset", "off"}:
        if allowed_chat_id is None:
            return thinking_lock_message()
        write_config(runtime.config_section, "reasoning_effort", None, root)
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
    write_config(runtime.config_section, "reasoning_effort", choice, root)
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


def thinking_lock_message(chat_provider: str = "chat") -> str:
    return (
        f"Enoch needs {_provider_label(chat_provider)} to be locked to one conversation "
        "before changing her thinking level."
    )


def config_command(
    text: str,
    root: Path,
    *,
    prefix: str = "/",
    runtime: AgentRuntime | None = None,
) -> str:
    runtime = runtime or _default_runtime(root)
    try:
        parts = shlex.split(text)
    except ValueError:
        return config_usage(prefix=prefix)
    if len(parts) == 1:
        return config_status(root, prefix=prefix, runtime=runtime)
    setting = parts[1].lower().replace("_", "-")
    if setting in {"provider", "providers"}:
        if len(parts) == 2:
            return provider_config_status(root, prefix=prefix)
        if len(parts) != 4 or setting != "provider":
            return config_usage(prefix=prefix)
        kind = parts[2].strip().lower()
        if kind not in PROVIDER_KINDS:
            return f"Provider kind must be one of: {', '.join(PROVIDER_KINDS)}."
        value = parts[3].strip().lower()
        if value in {"default", "reset"}:
            write_section_value("providers", kind, None, root)
            try:
                selected = provider_name(kind, root)
            except ProviderError as error:
                return str(error)
            message = f"Enoch {kind} provider reset to {selected}."
        else:
            try:
                choices = available_providers(kind, root)
            except ProviderError as error:
                return str(error)
            if value not in choices:
                return (
                    f"Unknown {kind} provider {value}. "
                    f"Available: {', '.join(choices) or 'none'}."
                )
            write_section_value("providers", kind, value, root)
            message = f"Enoch {kind} provider set to {value}."
        return "\n\n".join([message, provider_config_status(root, prefix=prefix)])
    if setting == "runtime":
        if (
            len(parts) < 4
            or parts[2].strip().lower() != "codex"
            or parts[3].strip().lower().replace("_", "-") != "executable"
        ):
            return config_usage(prefix=prefix)
        if len(parts) == 4:
            return codex_executable_config_status(root, prefix=prefix)
        if len(parts) != 5:
            return config_usage(prefix=prefix)
        value = parts[4].strip()
        if value.lower() in {"auto", "default", "reset"}:
            write_section_value("codex", "executable", None, root)
            message = "Enoch Codex executable reset to automatic discovery."
        else:
            candidate = resolve_codex_executable_value(value)
            if candidate.path is None:
                return f"Enoch could not set the Codex executable: {candidate.detail}"
            write_section_value("codex", "executable", value, root)
            message = f"Enoch Codex executable set to {candidate.path}."
        return "\n\n".join(
            [message, codex_executable_config_status(root, prefix=prefix)]
        )
    if setting == "task-timeout":
        if len(parts) == 2:
            return config_status(root, prefix=prefix, runtime=runtime)
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
                config_status(root, prefix=prefix, runtime=runtime),
            ]
        )
    if setting == "model":
        if len(parts) == 2:
            return model_config_status(root, prefix=prefix, runtime=runtime)
        if len(parts) != 3:
            return config_usage(prefix=prefix)
        value = parts[2].strip()
        section = runtime.config_section
        runtime_label = runtime.name.title()
        if value.lower() in {"default", "reset"}:
            write_section_value(section, "model", None, root)
            message = f"Enoch cleared her local {runtime_label} model override."
        elif not _CODEX_MODEL_PATTERN.fullmatch(value):
            return f"{runtime_label} model must be one model identifier without spaces."
        else:
            write_section_value(section, "model", value, root)
            message = f"Enoch {runtime_label} model set to {value}."
        return "\n\n".join(
            [message, model_config_status(root, prefix=prefix, runtime=runtime)]
        )
    if setting == "reasoning-effort":
        if len(parts) == 2:
            return config_status(root, prefix=prefix, runtime=runtime)
        if len(parts) != 3:
            return config_usage(prefix=prefix)
        value = parts[2].strip().lower()
        section = runtime.config_section
        runtime_label = runtime.name.title()
        if value in {"default", "reset"}:
            write_section_value(section, "reasoning_effort", None, root)
            message = f"Enoch cleared her local {runtime_label} reasoning effort override."
        elif value not in REASONING_EFFORTS:
            return f"{runtime_label} reasoning effort must be low, medium, high, or default."
        else:
            write_section_value(section, "reasoning_effort", value, root)
            message = f"Enoch {runtime_label} reasoning effort set to {value}."
        return "\n\n".join(
            [message, config_status(root, prefix=prefix, runtime=runtime)]
        )
    return config_usage(prefix=prefix)


def config_status(
    root: Path,
    *,
    prefix: str = "/",
    runtime: AgentRuntime | None = None,
) -> str:
    runtime = runtime or _default_runtime(root)
    settings = task_settings(root)
    default = " (default)" if settings.uses_default_timeout else ""
    command = f"{prefix}config"
    lines = [
        "Enoch config:",
        f"- Task timeout: {format_task_timeout(settings.timeout_seconds)}{default}",
        f"- Providers: {_provider_summary(root)}",
        "",
        f"{runtime.name.title()}:",
        runtime.model_summary(root),
    ]
    if runtime.name == "codex":
        resolution = resolve_codex_executable(root)
        lines.extend(
            [
                f"Executable: {resolution.path or 'not found'}",
                f"Executable source: {resolution.source}",
            ]
        )
    lines.extend(
        [
            "",
            f"Use {command} model to see available models or set one with {command} model <name>.",
            (
                f"Set reasoning with {command} reasoning-effort low|medium|high "
                f"or {command} reasoning-effort default."
            ),
            f"Set task timeout with {command} task-timeout <duration> or {command} task-timeout default.",
        ]
    )
    if runtime.name == "codex":
        lines.append(
            f"Set the runtime with {command} runtime codex executable <path|auto>."
        )
    return "\n".join(lines)


def model_config_status(
    root: Path,
    *,
    prefix: str = "/",
    runtime: AgentRuntime | None = None,
) -> str:
    runtime = runtime or _default_runtime(root)
    command = f"{prefix}config"
    summary = runtime.model_summary(root)
    current = _model_name_from_summary(summary)
    runtime_label = runtime.name.title()
    all_options = runtime.model_options()
    if runtime.name == "codex":
        options = tuple(
            option
            for option in all_options
            if option.slug == "gpt-5.6" or option.slug.startswith("gpt-5.6-")
        )
        available_label = "Available GPT-5.6 models:"
        example = f"Example: {command} model gpt-5.6-sol"
    else:
        options = all_options
        available_label = f"Available {runtime_label} models:"
        example = (
            f"Set with {command} model <name>."
            if not options
            else f"Example: {command} model {options[0].slug}"
        )
    lines = [f"{runtime_label} model:", summary, "", available_label]
    if options:
        for option in options:
            current_label = " [current]" if option.slug == current else ""
            description = f" - {option.description}" if option.description else ""
            lines.append(f"- {option.slug}{current_label}{description}")
    else:
        lines.append(
            f"- unavailable; Enoch could not find compatible models in the installed {runtime_label} catalog"
        )
    lines.extend(
        [
            "",
            example,
            f"Use {command} model default to inherit the {runtime_label} default.",
            f"Other valid model ids are accepted for private or future {runtime_label} rollouts.",
        ]
    )
    return "\n".join(lines)


def provider_config_status(root: Path, *, prefix: str = "/") -> str:
    command = f"{prefix}config"
    lines = ["Enoch providers:"]
    for kind in PROVIDER_KINDS:
        selected = provider_name(kind, root)
        choices = ", ".join(available_providers(kind, root))
        lines.append(f"- {kind}: {selected} (available: {choices})")
    lines.extend(
        [
            "",
            f"Set with {command} provider <chat|runtime|vcs|forge|service> <name>.",
            f"Reset with {command} provider <kind> default.",
            "Restart Enoch after changing a provider.",
        ]
    )
    return "\n".join(lines)


def codex_executable_config_status(root: Path, *, prefix: str = "/") -> str:
    command = f"{prefix}config"
    resolution = resolve_codex_executable(root)
    lines = [
        "Codex runtime executable:",
        f"- Executable: {resolution.path or 'not found'}",
        f"- Source: {resolution.source}",
    ]
    if resolution.detail:
        lines.append(f"- Detail: {resolution.detail}")
    lines.extend(
        [
            "",
            f"Set with {command} runtime codex executable <path>.",
            f"Reset with {command} runtime codex executable auto.",
        ]
    )
    return "\n".join(lines)


def _provider_summary(root: Path) -> str:
    providers = []
    for kind in PROVIDER_KINDS:
        try:
            selected = provider_name(kind, root)
        except ProviderError:
            selected = "not configured"
        providers.append(f"{kind}={selected}")
    return ", ".join(providers)


def _default_runtime(root: Path | None = None) -> FunctionAgentRuntime:
    return FunctionAgentRuntime(
        respond_fn=lambda *_args, **_kwargs: "",
        act_in_session_fn=lambda *_args, **_kwargs: "",
        model_summary_fn=model_summary,
        model_options_fn=lambda: codex_model_options(root),
        reset_usage_fn=lambda: None,
    )


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
            f"{command} providers - show active and available providers",
            f"{command} provider <kind> <name|default> - select a provider",
            f"{command} runtime codex executable - show the effective Codex executable",
            f"{command} runtime codex executable <path> - set the Codex executable",
            f"{command} runtime codex executable auto - restore automatic discovery",
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


def help_message(topic: str = "", *, chat_provider: str = "chat") -> str:
    normalized_topic = _normalize_help_topic(topic)
    if normalized_topic:
        topic_message = _help_topic_message(normalized_topic)
        if topic_message:
            return topic_message
        return f"No help found for /{normalized_topic}.\nUse /help to see available commands."
    return "\n".join(
        [
            "Enoch commands:",
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
            "Vision:",
            "Send a photo or JPEG, PNG, or WebP image document for Enoch to inspect",
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
            "/pr - list and manage pull requests",
            "/update - pull latest main, run doctor, and restart if safe",
            "/restart - restart Enoch's chat daemon from the locked conversation",
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
                "/evolve reconcile <id> [backfill] - verify promotion of a completed candidate",
                "/evolve remove <id> - remove a self-evolution candidate",
                "/evolve schedule <text> - let Enoch interpret common schedule text",
            ]
        )
    if topic == "config":
        return config_usage("/")
    if topic == "pr":
        return pr_usage("/")
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
                "/task resume <id|all> - continue paused tasks with the same ids",
                "/task retry <id> - retry a failed task as a new linked task",
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
        "restart": "/restart - restart Enoch's chat daemon from the locked conversation",
        "thinking": thinking_usage("/"),
    }
    return topics.get(topic, "")


def pr_usage(prefix: str = "/") -> str:
    return "\n".join(
        [
            "Pull request commands:",
            f"{prefix}pr - list open pull requests in the current repository",
            f"{prefix}pr show <PR number or PR URL> - inspect one pull request",
            f"{prefix}pr merge <PR number or PR URL> - inspect and merge exactly that PR",
            "A merge target is required; Enoch will not infer one from the current branch or conversation.",
        ]
    )


def action_lock_message(chat_provider: str = "chat") -> str:
    label = _provider_label(chat_provider)
    setup = f"Configure the {label} provider with a locked conversation and restart Enoch."
    return "\n".join(
        [
            f"Enoch will not change code or coordinate repository changes unless {label} is locked to one conversation.",
            setup,
        ]
    )


def _provider_label(name: str) -> str:
    cleaned = " ".join(part for part in re.split(r"[-_]", name.strip()) if part)
    return cleaned.title() or "Chat"
