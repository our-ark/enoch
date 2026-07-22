from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from our_ark_telegram.core import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)


def create_provider(root: Path | None = None) -> TelegramClient:
    return TelegramClient(load_config(root))


def load_config(root: Path | None = None) -> TelegramConfig:
    try:
        from enoch.config import read_section
    except ImportError as error:
        raise TelegramError("Telegram's Enoch integration requires the enoch package.") from error

    settings = read_section("telegram", root)
    token = os.environ.get("ENOCH_TELEGRAM_BOT_TOKEN") or settings.get("bot_token", "")
    if not token:
        raise TelegramError(
            "Configure telegram.bot_token or set ENOCH_TELEGRAM_BOT_TOKEN before starting Enoch."
        )
    allowed_chat_id = _optional_integer(
        os.environ.get("ENOCH_TELEGRAM_ALLOWED_CHAT_ID")
        or settings.get("allowed_chat_id", ""),
        name="Telegram allowed chat id",
    )
    poll_timeout = _integer(
        os.environ.get("ENOCH_TELEGRAM_POLL_TIMEOUT")
        or settings.get("poll_timeout", "")
        or str(DEFAULT_TELEGRAM_POLL_TIMEOUT),
        name="Telegram poll timeout",
    )
    return TelegramConfig(
        token=token,
        allowed_chat_id=allowed_chat_id,
        poll_timeout=poll_timeout,
    )


def setup_provider(
    text: str,
    root: Path,
    *,
    prompt: Callable[[str], str] | None = None,
    prefix: str = "",
) -> str:
    from enoch.config import config_path, read_section, write_section_value

    prompt_fn = prompt or input
    command = text.strip()
    parts = command.split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    if action == "setup-token":
        action = "token"
    elif action == "setup-chat":
        action = "chat"
    elif action == "setup":
        action = ""
    if action in {"help", "-h", "--help"}:
        return _setup_usage(prefix)
    if action in {"show", "status"}:
        return _setup_status(root)
    if action in {"token", "bot-token"}:
        token = argument or prompt_fn("Telegram bot token: ").strip()
        if not token:
            return "Telegram bot token was not saved."
        write_section_value("telegram", "bot_token", token, root)
        saved = f"Telegram bot token saved to {config_path(root)}."
        return "\n".join([saved, _next_step(root, prefix)])
    if action in {"chat", "chat-id", "allowed-chat-id"}:
        try:
            int(argument)
        except ValueError:
            return "Telegram chat id must be a whole number."
        if not argument:
            return "Use setup chat <chat_id>."
        write_section_value("telegram", "allowed_chat_id", argument, root)
        return "\n".join(
            [
                f"Telegram chat lock saved to {config_path(root)}.",
                "Restart Enoch so the daemon uses the new conversation lock:",
                "bin/enoch-daemon restart",
            ]
        )
    if action in {"poll", "poll-timeout", "timeout"}:
        try:
            timeout = int(argument)
        except ValueError:
            return "Telegram poll timeout must be a whole number of seconds."
        if timeout < 1:
            return "Telegram poll timeout must be at least 1 second."
        write_section_value("telegram", "poll_timeout", str(timeout), root)
        return f"Telegram poll timeout saved to {config_path(root)}."
    if not action:
        settings = read_section("telegram", root)
        saved = ""
        if not settings.get("bot_token", "").strip():
            token = prompt_fn("Telegram bot token: ").strip()
            if token:
                write_section_value("telegram", "bot_token", token, root)
                saved = f"Telegram bot token saved to {config_path(root)}."
        return "\n\n".join(part for part in (saved, _setup_status(root), _next_step(root, prefix)) if part)
    return _setup_usage(prefix)


def _setup_status(root: Path) -> str:
    from enoch.config import config_path, read_section

    settings = read_section("telegram", root)
    token = settings.get("bot_token", "").strip()
    conversation = settings.get("allowed_chat_id", "").strip()
    timeout = settings.get("poll_timeout", "").strip() or str(DEFAULT_TELEGRAM_POLL_TIMEOUT)
    return "\n".join(
        [
            "Telegram provider setup:",
            f"- config: {config_path(root)}",
            f"- bot token: {'saved' if token else 'missing'}",
            f"- conversation lock: {conversation or 'not set'}",
            f"- poll timeout: {timeout}",
        ]
    )


def _setup_usage(prefix: str) -> str:
    command = f"{prefix}setup" if prefix else "bin/enoch setup"
    return "\n".join(
        [
            "Telegram provider setup:",
            f"{command} show",
            f"{command} token <token>",
            f"{command} chat <chat_id>",
            f"{command} poll-timeout <seconds>",
        ]
    )


def _next_step(root: Path, prefix: str) -> str:
    from enoch.config import read_section

    settings = read_section("telegram", root)
    conversation = settings.get("allowed_chat_id", "").strip()
    command = f"{prefix}setup" if prefix else "bin/enoch setup"
    if not conversation:
        return "\n".join(
            [
                "Next:",
                "1. Start Enoch: bin/enoch-daemon start",
                "2. Send /status to the configured bot.",
                f"3. Save the conversation lock: {command} chat <chat_id>",
            ]
        )
    return "Setup is ready. Start or restart Enoch with: bin/enoch-daemon restart"


def _optional_integer(value: str, *, name: str) -> int | None:
    if not value.strip():
        return None
    return _integer(value, name=name)


def _integer(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise TelegramError(f"{name} must be a whole number.") from error
    if parsed < 1:
        raise TelegramError(f"{name} must be at least 1.")
    return parsed
