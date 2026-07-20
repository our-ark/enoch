"""Enoch configuration adapter for the shared Telegram transport."""

from __future__ import annotations

import os
from pathlib import Path

from enoch.config import config_path, read_section
from enoch.runtime import DEFAULT_TELEGRAM_POLL_TIMEOUT
from enoch.runtime_dependencies import activate_runtime_dependencies


activate_runtime_dependencies()

from our_ark_telegram import (  # noqa: E402
    MAX_TELEGRAM_MESSAGE,
    READ_ACK_EMOJI,
    TELEGRAM_API,
    TelegramClient,
    TelegramConfig,
    TelegramError,
    chunks,
    telegram_event,
)


def load_config(root: Path | None = None) -> TelegramConfig:
    settings = read_section("telegram", root)
    token = _setting(settings, "bot_token", "ENOCH_TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError(
            f"Run `bin/enoch setup-token <token>` or set ENOCH_TELEGRAM_BOT_TOKEN before starting Telegram. "
            f"Local config path: {config_path(root)}"
        )
    allowed_chat_id = _optional_int(
        _setting(settings, "allowed_chat_id", "ENOCH_TELEGRAM_ALLOWED_CHAT_ID"),
        name="Telegram allowed chat id",
    )
    poll_timeout = _positive_int(
        _setting(settings, "poll_timeout", "ENOCH_TELEGRAM_POLL_TIMEOUT")
        or str(DEFAULT_TELEGRAM_POLL_TIMEOUT),
        name="Telegram poll timeout",
    )
    return TelegramConfig(
        token=token,
        allowed_chat_id=allowed_chat_id,
        poll_timeout=poll_timeout,
    )


def _optional_int(value: str | None, *, name: str) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError as error:
        raise TelegramError(f"{name} must be a whole number.") from error


def _positive_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise TelegramError(f"{name} must be a whole number.") from error
    if parsed < 1:
        raise TelegramError(f"{name} must be at least 1.")
    return parsed


def _setting(settings: dict[str, str], key: str, env_name: str) -> str:
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return env_value.strip()
    return settings.get(key, "").strip()


__all__ = [
    "MAX_TELEGRAM_MESSAGE",
    "READ_ACK_EMOJI",
    "TELEGRAM_API",
    "TelegramClient",
    "TelegramConfig",
    "TelegramError",
    "chunks",
    "load_config",
    "telegram_event",
]
