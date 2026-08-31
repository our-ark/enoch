from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from our_ark_provider_kit import agent_context
from our_ark_slack.core import SlackClient, SlackConfig, SlackError


def create_provider(root: Path | None = None) -> SlackClient:
    context = agent_context(root)
    private_state_path = context.module("paths").private_state_path
    state_dir = private_state_path(Path("channels") / "slack" / "intake", root)
    return SlackClient(load_config(root), state_dir)


def load_config(root: Path | None = None) -> SlackConfig:
    context = agent_context(root)
    settings = context.module("config").read_section("slack", root)
    prefix = context.env_prefix
    bot_token = _setting(
        settings,
        "bot_token",
        f"{prefix}_SLACK_BOT_TOKEN",
        "OUR_ARK_SLACK_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
    )
    app_token = _setting(
        settings,
        "app_token",
        f"{prefix}_SLACK_APP_TOKEN",
        "OUR_ARK_SLACK_APP_TOKEN",
        "SLACK_APP_TOKEN",
    )
    if not bot_token or not app_token:
        raise SlackError(
            "Configure slack.bot_token and slack.app_token or set the matching "
            f"{prefix}_SLACK_* environment variables before starting {context.name}."
        )
    timeout_text = _setting(
        settings,
        "receive_timeout",
        f"{prefix}_SLACK_RECEIVE_TIMEOUT",
        "OUR_ARK_SLACK_RECEIVE_TIMEOUT",
    ) or "30"
    try:
        timeout = int(timeout_text)
    except ValueError as error:
        raise SlackError("Slack receive timeout must be a whole number.") from error
    try:
        return SlackConfig(
            bot_token=bot_token,
            app_token=app_token,
            allowed_conversation_id=_setting(
                settings,
                "allowed_conversation_id",
                f"{prefix}_SLACK_ALLOWED_CONVERSATION_ID",
                "OUR_ARK_SLACK_ALLOWED_CONVERSATION_ID",
            )
            or None,
            allowed_user_id=_setting(
                settings,
                "allowed_user_id",
                f"{prefix}_SLACK_ALLOWED_USER_ID",
                "OUR_ARK_SLACK_ALLOWED_USER_ID",
            )
            or None,
            receive_timeout=timeout,
        )
    except ValueError as error:
        raise SlackError(str(error)) from error


def setup_provider(
    text: str,
    root: Path,
    *,
    prompt: Callable[[str], str] | None = None,
    prefix: str = "",
) -> str:
    context = agent_context(root)
    config = context.module("config")
    prompt_fn = prompt or input
    parts = text.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    if action in {"help", "-h", "--help"}:
        return _setup_usage(prefix, context.service_slug)
    if action in {"show", "status"}:
        return _setup_status(root)
    if action in {"bot-token", "bot"}:
        value = argument or prompt_fn("Slack bot token: ").strip()
        return _save_secret(config, root, "bot_token", value, "Slack bot token")
    if action in {"app-token", "app"}:
        value = argument or prompt_fn("Slack app token: ").strip()
        return _save_secret(config, root, "app_token", value, "Slack app token")
    if action in {"conversation", "channel", "conversation-id"}:
        if not argument:
            return "Use setup conversation <conversation_id>."
        config.write_section_value("slack", "allowed_conversation_id", argument, root)
        return _restart_message(context.name, context.service_slug, "Slack conversation lock")
    if action in {"user", "user-id"}:
        if not argument:
            return "Use setup user <user_id>."
        config.write_section_value("slack", "allowed_user_id", argument, root)
        return _restart_message(context.name, context.service_slug, "Slack user lock")
    if action in {"timeout", "receive-timeout"}:
        try:
            timeout = int(argument)
        except ValueError:
            return "Slack receive timeout must be a whole number of seconds."
        if timeout < 1:
            return "Slack receive timeout must be at least 1 second."
        config.write_section_value("slack", "receive_timeout", str(timeout), root)
        return f"Slack receive timeout saved to {config.config_path(root)}."
    if not action:
        saved = []
        settings = config.read_section("slack", root)
        if not settings.get("bot_token", "").strip():
            token = prompt_fn("Slack bot token: ").strip()
            if token:
                config.write_section_value("slack", "bot_token", token, root)
                saved.append("Slack bot token saved.")
        if not settings.get("app_token", "").strip():
            token = prompt_fn("Slack app token: ").strip()
            if token:
                config.write_section_value("slack", "app_token", token, root)
                saved.append("Slack app token saved.")
        return "\n\n".join([*saved, _setup_status(root)])
    return _setup_usage(prefix, context.service_slug)


def _setup_status(root: Path) -> str:
    context = agent_context(root)
    config = context.module("config")
    settings = config.read_section("slack", root)
    return "\n".join(
        [
            "Slack provider setup:",
            f"- config: {config.config_path(root)}",
            f"- bot token: {'saved' if settings.get('bot_token', '').strip() else 'missing'}",
            f"- app token: {'saved' if settings.get('app_token', '').strip() else 'missing'}",
            "- conversation lock: "
            + (settings.get("allowed_conversation_id", "").strip() or "not set"),
            "- user lock: "
            + (settings.get("allowed_user_id", "").strip() or "not set"),
            "- receive timeout: "
            + (settings.get("receive_timeout", "").strip() or "30"),
        ]
    )


def _setup_usage(prefix: str, service_slug: str) -> str:
    command = f"{prefix}setup" if prefix else f"bin/{service_slug} setup"
    return "\n".join(
        [
            "Slack provider setup:",
            f"{command} show",
            f"{command} bot-token <xoxb-token>",
            f"{command} app-token <xapp-token>",
            f"{command} conversation <conversation-id>",
            f"{command} user <user-id>",
            f"{command} receive-timeout <seconds>",
        ]
    )


def _save_secret(config, root: Path, key: str, value: str, label: str) -> str:
    if not value:
        return f"{label} was not saved."
    config.write_section_value("slack", key, value, root)
    return f"{label} saved to {config.config_path(root)}."


def _restart_message(name: str, service_slug: str, setting: str) -> str:
    return "\n".join(
        [
            f"{setting} saved.",
            f"Restart {name} so the daemon uses it:",
            f"bin/{service_slug}-daemon restart",
        ]
    )


def _setting(settings: dict[str, str], key: str, *environment_names: str) -> str:
    for name in environment_names:
        if value := os.environ.get(name, "").strip():
            return value
    return settings.get(key, "").strip()
