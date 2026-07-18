from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib import parse, request

from enoch.config import config_path, read_section
from enoch.providers.contracts import ChatEvent, ChatProviderError, ConversationId
from enoch.runtime import DEFAULT_TELEGRAM_POLL_TIMEOUT, MAX_TELEGRAM_MESSAGE


TELEGRAM_API = "https://api.telegram.org"
READ_ACK_EMOJI = "👀"


class TelegramError(ChatProviderError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    allowed_chat_id: int | None = None
    poll_timeout: int = DEFAULT_TELEGRAM_POLL_TIMEOUT


class TelegramClient:
    name = "telegram"
    provider_kind = "chat"

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config

    @property
    def allowed_conversation_id(self) -> ConversationId | None:
        return self.config.allowed_chat_id

    def receive(self, cursor: int | None = None) -> list[ChatEvent]:
        return [
            event
            for update in self.get_updates(cursor)
            if (event := telegram_event(update)) is not None
        ]

    def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": self.config.poll_timeout}
        if offset is not None:
            payload["offset"] = offset
        data = self._call("getUpdates", payload)
        return list(data.get("result", []))

    def send_message(self, chat_id: int, text: str) -> int | None:
        first_message_id: int | None = None
        for chunk in chunks(text, MAX_TELEGRAM_MESSAGE):
            data = self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
            message_id = _message_id(data)
            if first_message_id is None and message_id is not None:
                first_message_id = message_id
        return first_message_id

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})

    def send_read_ack(self, chat_id: int, message_id: int) -> None:
        reaction = json.dumps([{"type": "emoji", "emoji": READ_ACK_EMOJI}], ensure_ascii=False)
        self._call(
            "setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": reaction},
        )

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{TELEGRAM_API}/bot{self.config.token}/{method}"
        body = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=body, method="POST")
        with request.urlopen(req, timeout=self.config.poll_timeout + 10) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise TelegramError(str(data))
        return data


def telegram_event(update: dict[str, Any]) -> ChatEvent | None:
    update_id = update.get("update_id")
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    text = str(message.get("text") or "").strip()
    if not isinstance(update_id, int) or not isinstance(chat_id, int) or not text:
        return None
    return ChatEvent(
        cursor=update_id + 1,
        conversation_id=chat_id,
        message_id=message_id if isinstance(message_id, int) else None,
        text=text,
        replied_text=_replied_text(message),
        raw=update,
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
    return TelegramConfig(token=token, allowed_chat_id=allowed_chat_id, poll_timeout=poll_timeout)


def chunks(text: str, size: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    if len(text) <= size:
        return [text]
    return [text[index : index + size] for index in range(0, len(text), size)]


def _message_id(data: dict[str, Any]) -> int | None:
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    return message_id if isinstance(message_id, int) else None


def _replied_text(message: dict[str, Any]) -> str:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return ""
    for key in ("text", "caption"):
        value = reply.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
