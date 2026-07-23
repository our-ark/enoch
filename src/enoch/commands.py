from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
from typing import Callable

from enoch.brain import (
    REASONING_EFFORTS,
    model_summary,
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
    load_provider,
    provider_name,
)
from enoch.profiles import ProfileError, available_profiles, load_profile
from enoch.skills import skills_command
from enoch.tasks.config import (
    format_task_timeout,
    parse_task_timeout,
    save_task_timeout,
    task_settings,
)


_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


ModelSummaryFn = Callable[[Path], str]
DoctorFn = Callable[[Path], ImmuneResult]
ResolveLineageFn = Callable[[Path], LineageResolution]
RefreshLineageFn = Callable[..., LineageInboxReport]
FormatDoctorFn = Callable[[ImmuneResult], str]
CommandUsageFn = Callable[[str], str]


@dataclass(frozen=True)
class CoreCommand:
    name: str
    handler: str
    section: str
    synopsis: str
    summary: str
    usage: CommandUsageFn | None = None

    @property
    def command(self) -> str:
        return f"/{self.name}"

    def summary_line(self, prefix: str = "/") -> str:
        synopsis = f" {self.synopsis}" if self.synopsis else ""
        return f"{prefix}{self.name}{synopsis} - {self.summary}"

    def usage_message(self, prefix: str = "/") -> str:
        if self.usage is not None:
            return self.usage(prefix)
        return self.summary_line(prefix)


def status_message(
    identity: Identity,
    root: Path,
    *,
    allowed_chat_id: ConversationId | None,
    chat_id: ConversationId | None = None,
    chat_provider: str = "chat",
    profile_name: str = "enoch",
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
            f"- Agent profile: {profile_name}",
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
    active_profile_name: str = "",
) -> str:
    runtime = runtime or _default_runtime(root)
    try:
        parts = shlex.split(text)
    except ValueError:
        return config_usage(prefix=prefix)
    if len(parts) == 1:
        return config_status(
            root,
            prefix=prefix,
            runtime=runtime,
            active_profile_name=active_profile_name,
        )
    setting = parts[1].lower().replace("_", "-")
    if setting in {"profile", "profiles"}:
        if len(parts) == 2:
            return profile_config_status(
                root,
                prefix=prefix,
                active_profile_name=active_profile_name,
            )
        if len(parts) != 3 or setting != "profile":
            return config_usage(prefix=prefix)
        value = parts[2].strip().lower()
        if value in {"default", "reset", "enoch"}:
            write_section_value("agent", "profile", None, root)
            message = "Enoch profile selection reset to the built-in enoch profile."
        else:
            try:
                load_profile(root, name=value)
            except ProfileError as error:
                return str(error)
            write_section_value("agent", "profile", value, root)
            message = f"Enoch profile set to {value}."
        return "\n\n".join(
            [
                message + " Restart Enoch to activate it.",
                profile_config_status(
                    root,
                    prefix=prefix,
                    active_profile_name=active_profile_name,
                ),
            ]
        )
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
        if len(parts) < 3:
            return config_usage(prefix=prefix)
        selected = parts[2].strip().lower()
        try:
            selected_runtime = load_provider("runtime", root, name=selected)
        except ProviderError as error:
            return str(error)
        configure = getattr(selected_runtime, "configure", None)
        if not callable(configure):
            return f"Runtime provider {selected} does not expose provider-specific config."
        return str(configure(tuple(parts[3:]), root, prefix=prefix))
    if setting == "task-timeout":
        if len(parts) == 2:
            return config_status(
                root,
                prefix=prefix,
                runtime=runtime,
                active_profile_name=active_profile_name,
            )
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
                config_status(
                    root,
                    prefix=prefix,
                    runtime=runtime,
                    active_profile_name=active_profile_name,
                ),
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
        elif not _MODEL_PATTERN.fullmatch(value):
            return f"{runtime_label} model must be one model identifier without spaces."
        else:
            write_section_value(section, "model", value, root)
            message = f"Enoch {runtime_label} model set to {value}."
        return "\n\n".join(
            [message, model_config_status(root, prefix=prefix, runtime=runtime)]
        )
    if setting == "reasoning-effort":
        if len(parts) == 2:
            return config_status(
                root,
                prefix=prefix,
                runtime=runtime,
                active_profile_name=active_profile_name,
            )
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
            [
                message,
                config_status(
                    root,
                    prefix=prefix,
                    runtime=runtime,
                    active_profile_name=active_profile_name,
                ),
            ]
        )
    return config_usage(prefix=prefix)


def config_status(
    root: Path,
    *,
    prefix: str = "/",
    runtime: AgentRuntime | None = None,
    active_profile_name: str = "",
) -> str:
    runtime = runtime or _default_runtime(root)
    settings = task_settings(root)
    default = " (default)" if settings.uses_default_timeout else ""
    command = f"{prefix}config"
    lines = [
        "Enoch config:",
        f"- Task timeout: {format_task_timeout(settings.timeout_seconds)}{default}",
        f"- Profile: {active_profile_name or _selected_profile_name(root)}",
        f"- Providers: {_provider_summary(root)}",
        "",
        f"{runtime.name.title()}:",
        runtime.model_summary(root),
    ]
    runtime_config_summary = getattr(runtime, "config_summary", None)
    if callable(runtime_config_summary):
        summary = str(runtime_config_summary(root)).strip()
        if summary:
            lines.append(summary)
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
    if callable(getattr(runtime, "configure", None)):
        lines.append(
            f"Use {command} runtime {runtime.name} to see provider-specific runtime settings."
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
    options = all_options
    available_label = str(
        getattr(runtime, "model_catalog_label", f"Available {runtime_label} models:")
    )
    example_model = str(getattr(runtime, "model_example", "")).strip()
    example = (
        f"Example: {command} model {example_model}"
        if example_model
        else (
            f"Set with {command} model <name>."
            if not options
            else f"Example: {command} model {options[0].slug}"
        )
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
        choices = ", ".join(available_providers(kind, root)) or "none"
        try:
            selected = provider_name(kind, root)
        except ProviderError:
            selected = "not configured"
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


def profile_config_status(
    root: Path,
    *,
    prefix: str = "/",
    active_profile_name: str = "",
) -> str:
    command = f"{prefix}config"
    selected = _selected_profile_name(root)
    running = active_profile_name.strip() or selected
    choices = ", ".join(available_profiles()) or "enoch"
    lines = [
        "Enoch profiles:",
        f"- Running: {running}",
        f"- Selected for restart: {selected}",
        f"- Available: {choices}",
        "",
        f"Set with {command} profile <name>.",
        f"Reset with {command} profile default.",
        "Restart Enoch after changing the profile.",
    ]
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


def _selected_profile_name(root: Path) -> str:
    try:
        return load_profile(root).name
    except ProfileError as error:
        return f"invalid ({error})"


def _default_runtime(root: Path | None = None) -> AgentRuntime:
    return load_provider("runtime", root)


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
            f"{command} profiles - show the running, selected, and available profiles",
            f"{command} profile <name|default> - select an agent profile for restart",
            f"{command} providers - show active and available providers",
            f"{command} provider <kind> <name|default> - select a provider",
            f"{command} runtime <provider> - show provider-specific runtime settings",
            f"{command} runtime <provider> <setting> [value] - configure the runtime provider",
            f"{command} model - show the effective and available runtime models",
            f"{command} model <name> - set a local runtime model override",
            f"{command} model default - inherit the runtime model",
            f"{command} reasoning-effort - show the effective reasoning effort",
            f"{command} reasoning-effort low|medium|high - set local reasoning effort",
            f"{command} reasoning-effort default - inherit runtime reasoning effort",
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
    del chat_provider
    normalized_topic = _normalize_help_topic(topic)
    if normalized_topic:
        command = core_command(normalized_topic)
        if command is not None:
            return command.usage_message("/")
        return "\n".join(
            [
                f"No help found for /{normalized_topic}.",
                "Use /help to see every command.",
                "Use /help <command> for detailed usage and subcommands.",
            ]
        )

    lines = [
        "Enoch commands:",
        "",
        "Use /help <command> for detailed usage and subcommands.",
        "Example: /help worktree",
    ]
    standalone = [command for command in CORE_COMMANDS if not command.section]
    if standalone:
        lines.extend(["", *(command.summary_line("/") for command in standalone)])
    sections = tuple(dict.fromkeys(command.section for command in CORE_COMMANDS if command.section))
    for section in sections:
        lines.extend(
            [
                "",
                f"{section}:",
                *(
                    command.summary_line("/")
                    for command in CORE_COMMANDS
                    if command.section == section
                ),
            ]
        )
    lines.extend(
        [
            "",
            "For repository changes, say the request naturally. "
            "Enoch will publish completed edits for review automatically.",
        ]
    )
    return "\n".join(lines)


def _normalize_help_topic(topic: str) -> str:
    stripped = topic.strip().lower()
    if not stripped:
        return ""
    first = stripped.split(maxsplit=1)[0]
    return first.lstrip("/")


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


def worktree_usage(prefix: str = "/") -> str:
    return "\n".join(
        [
            "Worktree commands:",
            f"{prefix}worktree - list isolated task worktrees and their branches",
            f"{prefix}worktree show <task-id> - inspect one task worktree",
            (
                f"{prefix}worktree cleanup <task-id> - remove only a clean inactive "
                "worktree; keep an unmerged local branch"
            ),
            (
                f"{prefix}worktree discard <task-id> force - permanently delete an inactive "
                "task worktree, its uncommitted changes, and its local branch"
            ),
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


def _help_usage(prefix: str) -> str:
    return "\n".join(
        [
            "Help commands:",
            f"{prefix}help - show every command",
            f"{prefix}help <command> - show detailed usage and subcommands",
            f"Example: {prefix}help worktree",
        ]
    )


def _start_usage(prefix: str) -> str:
    return "\n".join(
        [
            f"{prefix}start - show getting-started guidance",
            "This is a Telegram onboarding command. It does not start or restart the Enoch daemon.",
        ]
    )


def _task_usage(prefix: str) -> str:
    command = f"{prefix}task"
    return "\n".join(
        [
            "Task commands:",
            f"{command} <request> - queue background work for Enoch",
            f"{command} cancel <id> - cancel a queued background task",
            f"{command} resume <id|all> - continue paused tasks with the same ids",
            f"{command} retry <id> - retry a failed task as a new linked task",
        ]
    )


def _backlog_help_usage(prefix: str) -> str:
    command = f"{prefix}backlog"
    return "\n".join(
        [
            "Backlog commands:",
            f"{command} [p0|p1|p2] <request> - save deferred work",
            f"{command} remove <id> - remove a pending backlog item",
            f"{command} priority <id> p0|p1|p2 - reprioritize a pending backlog item",
            f"{command} promote <id> - move a pending backlog item into the active task queue",
        ]
    )


def _cron_help_usage(prefix: str) -> str:
    command = f"{prefix}cron"
    return "\n".join(
        [
            "Cron commands:",
            f"{command} every <interval> <request> - schedule recurring work",
            "Intervals can be like 10m, 2h, or 1d.",
            f"{command} cancel <id> - cancel a scheduled job",
            f"{command} - show scheduled jobs",
        ]
    )


def _evolve_help_usage(prefix: str) -> str:
    command = f"{prefix}evolve"
    return "\n".join(
        [
            "Evolve commands:",
            f"{command} - show self-evolution mode, theme, and top candidate",
            f"{command} mode <mode> - set self-evolution behavior",
            "Modes: disabled, co-evolve, auto-evolve.",
            f"{command} theme [text] - show or set the current self-evolution theme",
            f"{command} brainstorm - generate bounded candidates under the current theme",
            f"{command} list - show current self-evolution candidates",
            f"{command} approve <id> - approve and queue a self-evolution candidate",
            f"{command} retry <id> - retry a failed self-evolution candidate as a new task",
            f"{command} reconcile <id> [backfill] - verify promotion of a completed candidate",
            f"{command} remove <id> [reason] - remove a self-evolution candidate with an audit reason",
            f"{command} schedule <text> - let Enoch interpret common schedule text",
        ]
    )


CORE_COMMANDS = (
    CoreCommand("help", "help", "", "", "show this command list", _help_usage),
    CoreCommand(
        "start",
        "start",
        "Common",
        "",
        "show getting-started guidance",
        _start_usage,
    ),
    CoreCommand(
        "self",
        "self",
        "Common",
        "",
        "show Enoch's identity, role, ancestor, and mission",
    ),
    CoreCommand(
        "mission",
        "mission",
        "Common",
        "[text]",
        "show or update Enoch's mission",
    ),
    CoreCommand(
        "status",
        "status",
        "Common",
        "",
        "show identity, model, local state, and chat setup",
    ),
    CoreCommand("do", "do", "Work", "<request>", "run work now instead of queueing it"),
    CoreCommand(
        "task",
        "task",
        "Work",
        "<request>",
        "queue background work for Enoch",
        _task_usage,
    ),
    CoreCommand(
        "queue",
        "queue",
        "Work",
        "",
        "show running, queued, and recent task history",
    ),
    CoreCommand("stop", "stop", "Work", "", "stop the currently running task"),
    CoreCommand(
        "backlog",
        "backlog",
        "Work",
        "[p0|p1|p2] <request>",
        "save deferred work for idle time",
        _backlog_help_usage,
    ),
    CoreCommand("cron", "cron", "Work", "", "show scheduled jobs", _cron_help_usage),
    CoreCommand(
        "ancestors",
        "ancestors",
        "Inherit",
        "",
        "show ancestor chain and ancestor skills",
        lambda prefix: lineage_usage(prefix, command_name="ancestors"),
    ),
    CoreCommand(
        "inherit",
        "inherit",
        "Inherit",
        "",
        "show inheritable direct-parent changes",
        lambda prefix: lineage_usage(prefix, command_name="inherit"),
    ),
    CoreCommand(
        "skills",
        "skills",
        "Learn",
        "[agent-or-path]",
        "show declared skills",
    ),
    CoreCommand(
        "learn",
        "learn",
        "Learn",
        "<skill> from <agent>",
        "adapt a published skill from another agent",
    ),
    CoreCommand(
        "feedback",
        "feedback",
        "Evolve",
        "",
        "show feedback signals available to self-evolution",
    ),
    CoreCommand(
        "experience",
        "experience",
        "Evolve",
        "",
        "show task provenance statistics and evolution candidates",
    ),
    CoreCommand(
        "propose",
        "propose",
        "Evolve",
        "",
        "rank all evolve sources and propose the strongest candidate",
    ),
    CoreCommand(
        "evolve",
        "evolve",
        "Evolve",
        "",
        "show self-evolution mode, theme, and top candidate",
        _evolve_help_usage,
    ),
    CoreCommand(
        "config",
        "config",
        "System",
        "",
        "show or update local system settings",
        config_usage,
    ),
    CoreCommand("doctor", "doctor", "System", "", "run local health checks"),
    CoreCommand(
        "worktree",
        "worktree",
        "System",
        "",
        "inspect and manage isolated task worktrees",
        worktree_usage,
    ),
    CoreCommand(
        "pr",
        "pr",
        "System",
        "",
        "list and manage pull requests",
        pr_usage,
    ),
    CoreCommand(
        "update",
        "update",
        "System",
        "",
        "update from the authoritative repository, run doctor, and restart if safe",
    ),
    CoreCommand(
        "restart",
        "restart",
        "System",
        "",
        "restart Enoch's chat daemon from the locked conversation",
    ),
)

_CORE_COMMAND_INDEX = {command.name: command for command in CORE_COMMANDS}
if len(_CORE_COMMAND_INDEX) != len(CORE_COMMANDS):
    raise RuntimeError("Core command registry contains duplicate command names.")


def core_command(name: str) -> CoreCommand | None:
    normalized = name.strip().lower().lstrip("/")
    return _CORE_COMMAND_INDEX.get(normalized)


def core_command_names() -> frozenset[str]:
    return frozenset(_CORE_COMMAND_INDEX)
