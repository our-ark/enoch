from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from urllib import parse, request

from our_ark_provider_kit import (
    Attachment,
    ChatEvent,
    ChatProviderError,
    ConversationId,
    MessageId,
)
from our_ark_telegram.presentation import (
    is_formatting_error,
    render_telegram_html,
    telegram_message_chunks,
)


TELEGRAM_API = "https://api.telegram.org"
MAX_TELEGRAM_MESSAGE = 4096
DEFAULT_TELEGRAM_POLL_TIMEOUT = 30
READ_ACK_EMOJI = "👀"
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_PEER_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


class TelegramError(ChatProviderError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    allowed_chat_id: int | None = None
    poll_timeout: int = DEFAULT_TELEGRAM_POLL_TIMEOUT
    bot_peers: tuple["TelegramBotPeer", ...] = ()


@dataclass(frozen=True)
class TelegramBotPeer:
    """One explicitly trusted Telegram bot identity."""

    alias: str
    username: str
    user_id: int

    def __post_init__(self) -> None:
        alias = self.alias.strip().lower()
        username = self.username.strip().removeprefix("@")
        if not _PEER_ALIAS.fullmatch(alias):
            raise ValueError(
                "Telegram bot peer alias must start with a letter and contain only "
                "letters, numbers, underscores, or hyphens."
            )
        if not _BOT_USERNAME.fullmatch(username):
            raise ValueError("Telegram bot peer username is invalid.")
        if isinstance(self.user_id, bool) or int(self.user_id) < 1:
            raise ValueError("Telegram bot peer user id must be a positive integer.")
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "user_id", int(self.user_id))

    @property
    def address(self) -> str:
        return f"@{self.username}"

    @property
    def config_value(self) -> str:
        return f"{self.address}|{self.user_id}"


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

    def send_message(
        self,
        conversation_id: ConversationId,
        text: str,
    ) -> MessageId | None:
        first_message_id: int | None = None
        for chunk in telegram_message_chunks(text, MAX_TELEGRAM_MESSAGE):
            data = self._call_with_plain_fallback(
                "sendMessage",
                {
                    "chat_id": conversation_id,
                    "text": chunk.html,
                    "parse_mode": "HTML",
                },
                plain_text=chunk.plain,
            )
            message_id = _message_id(data)
            if first_message_id is None and message_id is not None:
                first_message_id = message_id
        return first_message_id

    def bot_peer(self, alias: str) -> TelegramBotPeer | None:
        normalized = alias.strip().lower()
        return next(
            (peer for peer in self.config.bot_peers if peer.alias == normalized),
            None,
        )

    def send_to_peer(self, alias: str, text: str) -> MessageId | None:
        peer = self.bot_peer(alias)
        if peer is None:
            raise TelegramError(f"Telegram bot peer {alias!r} is not allowed.")
        return self.send_message(peer.address, text)

    def peer_alias(self, event: ChatEvent) -> str | None:
        """Authenticate one private bot event against the configured peer list."""

        message = event.raw.get("message") if isinstance(event.raw, dict) else None
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        if chat.get("type") != "private" or sender.get("is_bot") is not True:
            return None
        sender_id = sender.get("id")
        username = str(sender.get("username") or "").strip().lower()
        if not isinstance(sender_id, int) or not username:
            return None
        for peer in self.config.bot_peers:
            if peer.user_id == sender_id and peer.username.lower() == username:
                return peer.alias
        return None

    def download_file(self, file_id: str, destination: Path, *, max_bytes: int) -> None:
        data = self._call("getFile", {"file_id": file_id})
        result = data.get("result") or {}
        file_path = str(result.get("file_path") or "").strip()
        if not file_path or file_path.startswith("/") or ".." in Path(file_path).parts:
            raise TelegramError("Telegram did not return a safe file path.")

        url = f"{TELEGRAM_API}/file/bot{self.config.token}/{file_path}"
        req = request.Request(url, method="GET")
        try:
            with request.urlopen(req, timeout=self.config.poll_timeout + 10) as response:
                declared_size = response.headers.get("Content-Length")
                if declared_size and int(declared_size) > max_bytes:
                    raise TelegramError("Telegram file is too large.")
                content = response.read(max_bytes + 1)
        except TelegramError:
            raise
        except (OSError, ValueError) as error:
            raise TelegramError("Could not download that Telegram file.") from error

        if len(content) > max_bytes:
            raise TelegramError("Telegram file is too large.")
        destination.write_bytes(content)

    def download_attachment(
        self,
        attachment: Attachment,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None:
        self.download_file(attachment.file_id, destination, max_bytes=max_bytes)

    def edit_message(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
        text: str,
    ) -> None:
        self._call_with_plain_fallback(
            "editMessageText",
            {
                "chat_id": conversation_id,
                "message_id": message_id,
                "text": render_telegram_html(text),
                "parse_mode": "HTML",
            },
            plain_text=text,
        )

    def send_read_ack(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
    ) -> None:
        reaction = json.dumps(
            [{"type": "emoji", "emoji": READ_ACK_EMOJI}],
            ensure_ascii=False,
        )
        self._call(
            "setMessageReaction",
            {
                "chat_id": conversation_id,
                "message_id": message_id,
                "reaction": reaction,
            },
        )

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{TELEGRAM_API}/bot{self.config.token}/{method}"
        body = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=body, method="POST")
        try:
            with request.urlopen(req, timeout=self.config.poll_timeout + 10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TelegramError(f"Telegram API call {method} failed.") from error
        if not data.get("ok"):
            raise TelegramError(str(data))
        return data

    def _call_with_plain_fallback(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        plain_text: str,
    ) -> dict[str, Any]:
        try:
            return self._call(method, payload)
        except TelegramError as error:
            if not is_formatting_error(error):
                raise
        fallback = dict(payload)
        fallback["text"] = plain_text
        fallback.pop("parse_mode", None)
        return self._call(method, fallback)


def telegram_event(update: dict[str, Any]) -> ChatEvent | None:
    update_id = update.get("update_id")
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    text = str(message.get("text") or message.get("caption") or "").strip()
    attachments = _attachments(message)
    has_supported_image = any(
        item.kind == "image" and item.file_id for item in attachments
    )
    if (
        not isinstance(update_id, int)
        or not isinstance(chat_id, int)
        or (not text and not has_supported_image)
    ):
        return None
    return ChatEvent(
        cursor=update_id + 1,
        conversation_id=chat_id,
        message_id=message_id if isinstance(message_id, int) else None,
        text=text,
        replied_text=_replied_text(message),
        raw=update,
        attachments=attachments,
    )


def chunks(text: str, size: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    if size < 1:
        raise ValueError("Chunk size must be at least 1.")
    if len(text) <= size:
        return [text]
    return [text[index : index + size] for index in range(0, len(text), size)]


def _attachments(message: dict[str, Any]) -> tuple[Attachment, ...]:
    attachments: list[Attachment] = []
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [
            item
            for item in photos
            if isinstance(item, dict) and item.get("file_id")
        ]
        if candidates:
            largest = max(
                candidates,
                key=lambda item: (
                    _integer(item.get("width")) * _integer(item.get("height")),
                    _integer(item.get("file_size")),
                ),
            )
            attachments.append(
                Attachment(
                    kind="image",
                    file_id=str(largest["file_id"]),
                    mime_type="image/jpeg",
                    size=_integer(largest.get("file_size")),
                    metadata={
                        "width": _integer(largest.get("width")),
                        "height": _integer(largest.get("height")),
                    },
                )
            )

    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        mime_type = str(document.get("mime_type") or "").lower()
        attachments.append(
            Attachment(
                kind="image" if mime_type in SUPPORTED_IMAGE_MIME_TYPES else "file",
                file_id=str(document["file_id"]),
                mime_type=mime_type,
                filename=str(document.get("file_name") or ""),
                size=_integer(document.get("file_size")),
            )
        )

    for field, kind in (("voice", "voice"), ("video", "video")):
        item = message.get(field)
        if isinstance(item, dict) and item.get("file_id"):
            attachments.append(
                Attachment(
                    kind=kind,
                    file_id=str(item["file_id"]),
                    mime_type=str(item.get("mime_type") or "").lower(),
                    size=_integer(item.get("file_size")),
                    metadata={"duration": _integer(item.get("duration"))},
                )
            )
    return tuple(attachments)


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


def _integer(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
