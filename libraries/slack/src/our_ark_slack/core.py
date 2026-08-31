from __future__ import annotations

import atexit
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable
from uuid import uuid4

from our_ark_provider_kit import (
    ChatEvent,
    ChatProviderError,
    ConversationId,
    MessageId,
    ProviderCapabilities,
)


MAX_SLACK_MARKDOWN = 12_000
SLACK_COMMAND = "/enoch"
READ_ACK_EMOJI = "eyes"
SPOOL_SCHEMA_VERSION = 1
_MENTION_PREFIX = re.compile(r"^<@[A-Z0-9]+>[:,]?\s*", re.IGNORECASE)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class SlackError(ChatProviderError):
    pass


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    app_token: str
    allowed_conversation_id: str | None = None
    allowed_user_id: str | None = None
    receive_timeout: int = 30

    def __post_init__(self) -> None:
        bot_token = self.bot_token.strip()
        app_token = self.app_token.strip()
        conversation = _optional_id(self.allowed_conversation_id)
        user = _optional_id(self.allowed_user_id)
        timeout = int(self.receive_timeout)
        if not bot_token:
            raise ValueError("Slack bot token is required.")
        if not app_token:
            raise ValueError("Slack app token is required.")
        if timeout < 1:
            raise ValueError("Slack receive timeout must be at least 1 second.")
        object.__setattr__(self, "bot_token", bot_token)
        object.__setattr__(self, "app_token", app_token)
        object.__setattr__(self, "allowed_conversation_id", conversation)
        object.__setattr__(self, "allowed_user_id", user)
        object.__setattr__(self, "receive_timeout", timeout)


class SlackClient:
    name = "slack"
    provider_kind = "chat"
    capabilities = ProviderCapabilities(
        provider_kind="chat",
        capabilities=frozenset(
            {"chat.receive", "chat.send", "chat.edit", "chat.ack"}
        ),
    )

    def __init__(
        self,
        config: SlackConfig,
        state_dir: Path,
        *,
        web_client: Any | None = None,
        socket_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self._web = web_client if web_client is not None else _create_web_client(config)
        self._socket_factory = socket_factory or self._create_socket_client
        self._socket: Any | None = None
        self._condition = threading.Condition(threading.RLock())
        self._listener_error = ""
        self._closed = False
        atexit.register(self.close)

    @property
    def allowed_conversation_id(self) -> ConversationId | None:
        return self.config.allowed_conversation_id

    def receive(self, cursor: int | str | None = None) -> list[ChatEvent]:
        offset = _cursor_value(cursor)
        with self._condition:
            pending = self._pending_events(offset)
            if pending:
                return pending
            self._ensure_connected()
            self._condition.wait(timeout=self.config.receive_timeout)
            pending = self._pending_events(offset)
            if pending:
                return pending
            if self._listener_error:
                error = self._listener_error
                self._listener_error = ""
                raise SlackError(error)
            return []

    def send_message(
        self,
        conversation_id: ConversationId,
        text: str,
    ) -> MessageId | None:
        first_message_id: str | None = None
        for chunk in slack_message_chunks(text):
            response = self._api_call(
                "chat.postMessage",
                self._web.chat_postMessage,
                channel=str(conversation_id),
                markdown_text=chunk,
            )
            message_id = _response_value(response, "ts")
            if first_message_id is None and message_id:
                first_message_id = message_id
        return first_message_id

    def edit_message(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
        text: str,
    ) -> None:
        rendered = _bounded_markdown(text)
        self._api_call(
            "chat.update",
            self._web.chat_update,
            channel=str(conversation_id),
            ts=str(message_id),
            markdown_text=rendered,
        )

    def send_read_ack(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
    ) -> None:
        try:
            self._api_call(
                "reactions.add",
                self._web.reactions_add,
                channel=str(conversation_id),
                timestamp=str(message_id),
                name=READ_ACK_EMOJI,
            )
        except SlackError as error:
            if "already_reacted" not in str(error):
                raise

    def close(self) -> None:
        socket = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            socket = self._socket
            self._socket = None
            self._condition.notify_all()
        if socket is not None:
            close = getattr(socket, "close", None)
            if callable(close):
                close()

    def _ensure_connected(self) -> None:
        if self._closed:
            raise SlackError("Slack provider is closed.")
        if self._socket is not None:
            return
        try:
            socket = self._socket_factory(self._web)
            socket.socket_mode_request_listeners.append(self._handle_request)
            socket.connect()
        except Exception as error:
            raise SlackError(f"Slack Socket Mode connection failed: {_error_detail(error)}") from error
        self._socket = socket

    def _create_socket_client(self, web_client: Any) -> Any:
        try:
            from slack_sdk.socket_mode.websocket_client import SocketModeClient
        except ImportError as error:
            raise SlackError(
                "Install our-ark-slack with its slack-sdk and websocket-client dependencies."
            ) from error
        return SocketModeClient(
            app_token=self.config.app_token,
            web_client=web_client,
            concurrency=2,
        )

    def _handle_request(self, socket: Any, request: Any) -> None:
        try:
            event = slack_event(
                str(getattr(request, "type", "")),
                getattr(request, "payload", {}),
                cursor=0,
                allowed_conversation_id=self.config.allowed_conversation_id,
                allowed_user_id=self.config.allowed_user_id,
            )
            if event is not None:
                self._store_request(request)
            socket.send_socket_mode_response(
                {"envelope_id": str(getattr(request, "envelope_id", ""))}
            )
        except Exception as error:
            with self._condition:
                self._listener_error = (
                    "Slack could not durably accept an incoming event: "
                    f"{_error_detail(error)}"
                )
                self._condition.notify_all()
            raise

    def _store_request(self, request: Any) -> None:
        request_type = str(getattr(request, "type", "")).strip()
        payload = _sanitized_payload(getattr(request, "payload", {}))
        identity = _event_identity(
            request_type,
            payload,
            str(getattr(request, "envelope_id", "")),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        path = self.state_dir / f"event-{digest}.json"
        with self._condition:
            if path.is_file():
                self._condition.notify_all()
                return
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
            sequence = self._allocate_sequence()
            _atomic_json(
                path,
                {
                    "schema_version": SPOOL_SCHEMA_VERSION,
                    "sequence": sequence,
                    "identity": identity,
                    "request_type": request_type,
                    "payload": payload,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._condition.notify_all()

    def _allocate_sequence(self) -> int:
        path = self.state_dir / "sequence.json"
        next_sequence = 1
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                next_sequence = max(1, int(data.get("next_sequence", 1)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise SlackError("Slack intake sequence state is unreadable.") from error
        _atomic_json(
            path,
            {
                "schema_version": SPOOL_SCHEMA_VERSION,
                "next_sequence": next_sequence + 1,
            },
        )
        return next_sequence

    def _pending_events(self, cursor: int) -> list[ChatEvent]:
        if not self.state_dir.is_dir():
            return []
        records: list[tuple[int, Path, dict[str, Any]]] = []
        for path in self.state_dir.glob("event-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                sequence = int(data["sequence"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise SlackError(f"Slack intake record {path.name} is unreadable.") from error
            records.append((sequence, path, data))
        records.sort(key=lambda item: item[0])
        self._prune(records, cursor)
        events = []
        for sequence, _path, data in records:
            if sequence <= cursor:
                continue
            event = slack_event(
                str(data.get("request_type") or ""),
                data.get("payload"),
                cursor=sequence,
                allowed_conversation_id=self.config.allowed_conversation_id,
                allowed_user_id=self.config.allowed_user_id,
            )
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _prune(records: list[tuple[int, Path, dict[str, Any]]], cursor: int) -> None:
        acknowledged = [record for record in records if record[0] <= cursor]
        for _sequence, path, _data in acknowledged[:-100]:
            path.unlink(missing_ok=True)

    @staticmethod
    def _api_call(operation: str, call: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            response = call(**kwargs)
        except Exception as error:
            raise SlackError(
                f"Slack API call {operation} failed: {_error_detail(error)}"
            ) from error
        if not bool(_response_value(response, "ok", True)):
            detail = _response_value(response, "error") or "unknown Slack error"
            raise SlackError(f"Slack API call {operation} failed: {detail}")
        return response


def slack_event(
    request_type: str,
    payload: object,
    *,
    cursor: int,
    allowed_conversation_id: str | None = None,
    allowed_user_id: str | None = None,
) -> ChatEvent | None:
    if not isinstance(payload, dict):
        return None
    request_kind = request_type.strip().lower()
    if request_kind == "slash_commands":
        return _slash_command_event(
            payload,
            cursor,
            allowed_conversation_id=allowed_conversation_id,
            allowed_user_id=allowed_user_id,
        )
    if request_kind != "events_api" or payload.get("type") != "event_callback":
        return None
    native = payload.get("event")
    if not isinstance(native, dict):
        return None
    event_type = str(native.get("type") or "")
    if event_type not in {"message", "app_mention"}:
        return None
    if native.get("bot_id") or native.get("bot_profile"):
        return None
    subtype = str(native.get("subtype") or "")
    if subtype and subtype not in {"file_share"}:
        return None
    conversation = _optional_id(native.get("channel"))
    user = _optional_id(native.get("user"))
    if not conversation or not user:
        return None
    if allowed_conversation_id and conversation != allowed_conversation_id:
        return None
    if allowed_user_id and user != allowed_user_id:
        return None
    text = str(native.get("text") or "").strip()
    if event_type == "app_mention":
        text = _MENTION_PREFIX.sub("", text).strip()
    if not text:
        return None
    message_id = _optional_id(native.get("ts"))
    return ChatEvent(
        cursor=cursor,
        conversation_id=conversation,
        message_id=message_id,
        text=text,
        raw=deepcopy(payload),
    )


def slack_message_chunks(text: str, size: int = MAX_SLACK_MARKDOWN) -> list[str]:
    if size < 1:
        raise ValueError("Slack chunk size must be at least 1.")
    value = str(text)
    if not value:
        return [""]
    chunks = []
    remaining = value
    while len(remaining) > size:
        boundary = max(
            remaining.rfind("\n\n", 0, size + 1),
            remaining.rfind("\n", 0, size + 1),
            remaining.rfind(" ", 0, size + 1),
        )
        if boundary < max(1, size // 2):
            boundary = size
        else:
            boundary += 2 if remaining[boundary : boundary + 2] == "\n\n" else 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    chunks.append(remaining)
    return chunks


def _slash_command_event(
    payload: dict[str, Any],
    cursor: int,
    *,
    allowed_conversation_id: str | None,
    allowed_user_id: str | None,
) -> ChatEvent | None:
    if str(payload.get("command") or "").strip().lower() != SLACK_COMMAND:
        return None
    conversation = _optional_id(payload.get("channel_id"))
    user = _optional_id(payload.get("user_id"))
    if not conversation or not user:
        return None
    if allowed_conversation_id and conversation != allowed_conversation_id:
        return None
    if allowed_user_id and user != allowed_user_id:
        return None
    argument = str(payload.get("text") or "").strip()
    if not argument:
        text = "/help"
    elif argument.startswith("/"):
        text = argument
    else:
        text = f"/{argument}"
    return ChatEvent(
        cursor=cursor,
        conversation_id=conversation,
        text=text,
        raw=deepcopy(payload),
    )


def _sanitized_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SlackError("Slack sent an invalid Socket Mode payload.")
    cleaned = deepcopy(payload)
    for key in ("token", "response_url"):
        cleaned.pop(key, None)
    return cleaned


def _bounded_markdown(text: str) -> str:
    value = str(text)
    if len(value) <= MAX_SLACK_MARKDOWN:
        return value
    marker = "\n\n[Message truncated to fit Slack.]"
    return value[: MAX_SLACK_MARKDOWN - len(marker)] + marker


def _event_identity(request_type: str, payload: dict[str, Any], envelope_id: str) -> str:
    for key in ("event_id", "trigger_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return f"{request_type}:{value}"
    stable = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if stable != "{}":
        return f"{request_type}:{hashlib.sha256(stable.encode('utf-8')).hexdigest()}"
    return f"{request_type}:{envelope_id}"


def _cursor_value(value: int | str | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise SlackError("Slack cursor must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise SlackError("Slack cursor must be a non-negative integer.") from error
    if parsed < 0:
        raise SlackError("Slack cursor must be a non-negative integer.")
    return parsed


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if not _SAFE_ID.fullmatch(cleaned):
        raise ValueError(f"Invalid Slack identifier {cleaned!r}.")
    return cleaned


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _response_value(response: Any, key: str, default: Any = "") -> Any:
    try:
        return response.get(key, default)
    except AttributeError:
        try:
            return response[key]
        except (KeyError, TypeError):
            return default


def _error_detail(error: BaseException) -> str:
    response = getattr(error, "response", None)
    detail = _response_value(response, "error") if response is not None else ""
    return str(detail or error or type(error).__name__)


def _create_web_client(config: SlackConfig) -> Any:
    try:
        from slack_sdk.web import WebClient
    except ImportError as error:
        raise SlackError(
            "Install our-ark-slack with its slack-sdk dependency."
        ) from error
    return WebClient(token=config.bot_token, timeout=config.receive_timeout)
