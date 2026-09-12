from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from our_ark_provider_kit import agent_context
from our_ark_telegram.core import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT,
    TelegramBotPeer,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)


def create_provider(root: Path | None = None) -> TelegramClient:
    return TelegramClient(load_config(root))


def load_config(root: Path | None = None) -> TelegramConfig:
    context = agent_context(root)
    read_section = context.module("config").read_section
    settings = read_section("telegram", root)
    token = (
        os.environ.get(f"{context.env_prefix}_TELEGRAM_BOT_TOKEN")
        or os.environ.get("OUR_ARK_TELEGRAM_BOT_TOKEN")
        or settings.get("bot_token", "")
    )
    if not token:
        raise TelegramError(
            "Configure telegram.bot_token or set "
            f"{context.env_prefix}_TELEGRAM_BOT_TOKEN before starting {context.name}."
        )
    allowed_chat_id = _optional_integer(
        os.environ.get(f"{context.env_prefix}_TELEGRAM_ALLOWED_CHAT_ID")
        or os.environ.get("OUR_ARK_TELEGRAM_ALLOWED_CHAT_ID")
        or settings.get("allowed_chat_id", ""),
        name="Telegram allowed chat id",
    )
    poll_timeout = _integer(
        os.environ.get(f"{context.env_prefix}_TELEGRAM_POLL_TIMEOUT")
        or os.environ.get("OUR_ARK_TELEGRAM_POLL_TIMEOUT")
        or settings.get("poll_timeout", "")
        or str(DEFAULT_TELEGRAM_POLL_TIMEOUT),
        name="Telegram poll timeout",
    )
    return TelegramConfig(
        token=token,
        allowed_chat_id=allowed_chat_id,
        poll_timeout=poll_timeout,
        bot_peers=_bot_peers(settings),
    )


def setup_provider(
    text: str,
    root: Path,
    *,
    prompt: Callable[[str], str] | None = None,
    prefix: str = "",
) -> str:
    context = agent_context(root)
    config = context.module("config")
    config_path = config.config_path
    read_section = config.read_section
    write_section_value = config.write_section_value

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
        return _setup_usage(prefix, context.service_slug)
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
                f"Restart {context.name} so the daemon uses the new conversation lock:",
                f"bin/{context.service_slug}-daemon restart",
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
    if action in {"peer", "bot-peer", "agent-peer"}:
        return _setup_peer(argument, root, prefix=prefix)
    if not action:
        settings = read_section("telegram", root)
        saved = ""
        if not settings.get("bot_token", "").strip():
            token = prompt_fn("Telegram bot token: ").strip()
            if token:
                write_section_value("telegram", "bot_token", token, root)
                saved = f"Telegram bot token saved to {config_path(root)}."
        return "\n\n".join(part for part in (saved, _setup_status(root), _next_step(root, prefix)) if part)
    return _setup_usage(prefix, context.service_slug)


def _setup_status(root: Path) -> str:
    config = agent_context(root).module("config")
    config_path = config.config_path
    read_section = config.read_section

    settings = read_section("telegram", root)
    token = settings.get("bot_token", "").strip()
    conversation = settings.get("allowed_chat_id", "").strip()
    timeout = settings.get("poll_timeout", "").strip() or str(DEFAULT_TELEGRAM_POLL_TIMEOUT)
    peers = _bot_peers(settings)
    return "\n".join(
        [
            "Telegram provider setup:",
            f"- config: {config_path(root)}",
            f"- bot token: {'saved' if token else 'missing'}",
            f"- conversation lock: {conversation or 'not set'}",
            f"- poll timeout: {timeout}",
            "- bot peers: "
            + (
                ", ".join(f"{peer.alias}={peer.address} ({peer.user_id})" for peer in peers)
                if peers
                else "none"
            ),
        ]
    )


def _setup_usage(prefix: str, service_slug: str) -> str:
    command = f"{prefix}setup" if prefix else f"bin/{service_slug} setup"
    return "\n".join(
        [
            "Telegram provider setup:",
            f"{command} show",
            f"{command} token <token>",
            f"{command} chat <chat_id>",
            f"{command} poll-timeout <seconds>",
            f"{command} peer <alias> <@username> <bot-user-id>",
            f"{command} peer remove <alias>",
        ]
    )


def _next_step(root: Path, prefix: str) -> str:
    context = agent_context(root)
    read_section = context.module("config").read_section

    settings = read_section("telegram", root)
    conversation = settings.get("allowed_chat_id", "").strip()
    command = f"{prefix}setup" if prefix else f"bin/{context.service_slug} setup"
    if not conversation:
        return "\n".join(
            [
                "Next:",
                f"1. Start {context.name}: bin/{context.service_slug}-daemon start",
                "2. Send /status to the configured bot.",
                f"3. Save the conversation lock: {command} chat <chat_id>",
            ]
        )
    return (
        f"Setup is ready. Start or restart {context.name} with: "
        f"bin/{context.service_slug}-daemon restart"
    )


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


def _bot_peers(settings: dict[str, str]) -> tuple[TelegramBotPeer, ...]:
    peers: list[TelegramBotPeer] = []
    for key, value in sorted(settings.items()):
        if not key.startswith("peer_"):
            continue
        username, separator, raw_id = value.strip().partition("|")
        if not separator:
            raise TelegramError(
                f"Telegram bot peer {key[5:]!r} must use @username|bot-user-id."
            )
        try:
            peer = TelegramBotPeer(key[5:], username, int(raw_id))
        except (TypeError, ValueError) as error:
            raise TelegramError(f"Invalid Telegram bot peer {key[5:]!r}: {error}") from error
        peers.append(peer)
    return tuple(peers)


def _setup_peer(argument: str, root: Path, *, prefix: str) -> str:
    context = agent_context(root)
    config = context.module("config")
    parts = argument.split()
    if parts and parts[0].lower() in {"remove", "clear", "delete"}:
        if len(parts) != 2:
            return _setup_usage(prefix, context.service_slug)
        try:
            alias = TelegramBotPeer(parts[1], "placeholder_bot", 1).alias
        except ValueError as error:
            return str(error)
        config.write_section_value("telegram", f"peer_{alias}", None, root)
        return f"Telegram bot peer {alias} removed. Restart {context.name} to apply it."
    if len(parts) != 3:
        return _setup_usage(prefix, context.service_slug)
    try:
        peer = TelegramBotPeer(parts[0], parts[1], int(parts[2]))
    except (TypeError, ValueError) as error:
        return str(error)
    config.write_section_value(
        "telegram",
        f"peer_{peer.alias}",
        peer.config_value,
        root,
    )
    return "\n".join(
        [
            f"Telegram bot peer {peer.alias} saved as {peer.address} ({peer.user_id}).",
            f"Restart {context.name} so the daemon loads the peer allowlist:",
            f"bin/{context.service_slug}-daemon restart",
        ]
    )
