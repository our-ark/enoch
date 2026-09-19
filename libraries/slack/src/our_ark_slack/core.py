from __future__ import annotations

import atexit
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Callable
import zipfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid4, uuid5

from our_ark_provider_kit import (
    Attachment,
    ChatEvent,
    ChatProviderError,
    ConversationId,
    MessageId,
    NotificationCapabilities,
    NotificationDeliveryError,
    NotificationIntent,
    NotificationReceipt,
    OutboundAttachment,
    ProviderCapabilities,
)


MAX_SLACK_MARKDOWN = 12_000
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_COMMAND_PREFIX = "."
SECONDARY_COMMAND_PREFIX = "!"
READ_ACK_EMOJI = "eyes"
SPOOL_SCHEMA_VERSION = 1
_MENTION_PREFIX = re.compile(r"^<@[A-Z0-9]+>[:,]?\s*", re.IGNORECASE)
_SECONDARY_COMMAND = re.compile(r"^[.!]([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*))?$", re.DOTALL)
_SLASH_COMMAND = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
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
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES

    def __post_init__(self) -> None:
        bot_token = self.bot_token.strip()
        app_token = self.app_token.strip()
        conversation = _optional_id(self.allowed_conversation_id)
        user = _optional_id(self.allowed_user_id)
        timeout = int(self.receive_timeout)
        max_upload_bytes = int(self.max_upload_bytes)
        if not bot_token:
            raise ValueError("Slack bot token is required.")
        if not app_token:
            raise ValueError("Slack app token is required.")
        if timeout < 1:
            raise ValueError("Slack receive timeout must be at least 1 second.")
        if max_upload_bytes < 1 or max_upload_bytes > 100 * 1024 * 1024:
            raise ValueError("Slack max upload bytes must be between 1 and 104857600.")
        object.__setattr__(self, "bot_token", bot_token)
        object.__setattr__(self, "app_token", app_token)
        object.__setattr__(self, "allowed_conversation_id", conversation)
        object.__setattr__(self, "allowed_user_id", user)
        object.__setattr__(self, "receive_timeout", timeout)
        object.__setattr__(self, "max_upload_bytes", max_upload_bytes)


class SlackClient:
    name = "slack"
    provider_kind = "chat"
    command_prefix = DEFAULT_COMMAND_PREFIX
    capabilities = ProviderCapabilities(
        provider_kind="chat",
        capabilities=frozenset(
            {
                "chat.receive", "chat.send", "chat.edit", "chat.ack",
                "chat.attachment", "chat.attach",
            }
        ),
    )
    notification_capabilities = NotificationCapabilities(
        idempotent_delivery=True,
        reconciliation=True,
        attachments=True,
    )

    def __init__(
        self,
        config: SlackConfig,
        state_dir: Path,
        *,
        web_client: Any | None = None,
        socket_factory: Callable[[Any], Any] | None = None,
        approved_artifact_roots: tuple[Path, ...] = (),
        binary_uploader: Callable[[str, bytes, str], None] | None = None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self._web = web_client if web_client is not None else _create_web_client(config)
        self._socket_factory = socket_factory or self._create_socket_client
        self._approved_artifact_roots = tuple(
            Path(path).expanduser().resolve() for path in approved_artifact_roots
        )
        self._binary_uploader = binary_uploader or _upload_binary
        self._outbound_state_dir = self.state_dir.parent / "outbound"
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

    def deliver_notification(self, intent: NotificationIntent) -> NotificationReceipt:
        if intent.operation == "edit":
            assert intent.message_id is not None
            self.edit_message(intent.conversation_id, intent.message_id, intent.text)
            return NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="delivered",
                message_id=intent.message_id,
            )
        if not intent.attachments:
            message_id = self._post_notification_text(intent)
            return NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="delivered",
                message_id=message_id,
            )
        return self._deliver_attachments(intent)

    def reconcile_notification(self, intent: NotificationIntent) -> NotificationReceipt:
        if not intent.attachments:
            return NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="unknown",
                detail="Slack text delivery cannot be reconciled without additional history scopes.",
            )
        state = self._load_outbound_state(intent)
        if state.get("status") == "delivered":
            return _state_receipt(intent, state)
        file_ids = _state_file_ids(state)
        if not file_ids:
            return NotificationReceipt(idempotency_key=intent.idempotency_key, status="not_found")
        try:
            shared = [self._slack_file_shared(file_id, str(intent.conversation_id)) for file_id in file_ids]
        except SlackError as error:
            return NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="unknown",
                detail=str(error),
            )
        if all(shared):
            state.update(
                status="completing",
                attachment_references=file_ids,
                provider_reference=f"slack-files:{','.join(file_ids)}",
            )
            self._write_outbound_state(intent, state)
            chunks = slack_message_chunks(intent.text) if intent.text else [""]
            return self._finish_attachment_delivery(intent, state, chunks)
        if not any(shared):
            return NotificationReceipt(idempotency_key=intent.idempotency_key, status="not_found")
        return NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="unknown",
            detail="Slack reported a partial attachment share; delivery was not repeated.",
        )

    def _post_notification_text(self, intent: NotificationIntent) -> MessageId | None:
        first_message_id: str | None = None
        for index, chunk in enumerate(slack_message_chunks(intent.text)):
            kwargs: dict[str, Any] = {
                "channel": str(intent.conversation_id),
                "markdown_text": chunk,
                "client_msg_id": str(uuid5(NAMESPACE_URL, f"{intent.idempotency_key}:{index}")),
            }
            if intent.thread_id is not None:
                kwargs["thread_ts"] = str(intent.thread_id)
            response = self._api_call("chat.postMessage", self._web.chat_postMessage, **kwargs)
            message_id = _response_value(response, "ts")
            if first_message_id is None and message_id:
                first_message_id = str(message_id)
        return first_message_id

    def _deliver_attachments(self, intent: NotificationIntent) -> NotificationReceipt:
        conversation = str(intent.conversation_id)
        if not self.config.allowed_conversation_id:
            return self._attachment_fallback(
                intent,
                "Attachment not sent because the Slack conversation lock is not configured.",
            )
        if conversation != self.config.allowed_conversation_id:
            raise NotificationDeliveryError(
                "Slack attachment destination is outside the governed conversation.",
                retryable=False,
            )
        state = self._load_outbound_state(intent)
        if state.get("status") == "delivered":
            return _state_receipt(intent, state)
        if state.get("status") == "completing":
            reconciled = self.reconcile_notification(intent)
            if reconciled.status == "delivered":
                return reconciled
            if reconciled.status == "unknown" and "partial" in reconciled.detail.lower():
                return self._partial_attachment_fallback(intent, state)
            if reconciled.status == "unknown":
                raise NotificationDeliveryError(
                    "Slack could not reconcile an interrupted attachment upload; "
                    "ensure the app retains files:read.",
                    retryable=True,
                    ambiguous=True,
                )
        attempts = int(state.get("attempts") or 0) + 1
        state["attempts"] = attempts
        self._write_outbound_state(intent, state)
        try:
            payloads = [self._verified_artifact(value) for value in intent.attachments]
            files_state = state.setdefault("files", {})
            for attachment, data in zip(intent.attachments, payloads):
                entry = files_state.setdefault(attachment.id, {})
                if entry.get("stage") == "uploaded" and entry.get("file_id"):
                    continue
                response = self._api_call(
                    "files.getUploadURLExternal",
                    self._web.files_getUploadURLExternal,
                    filename=attachment.filename,
                    length=attachment.size,
                    **({"alt_txt": attachment.filename} if attachment.kind == "image" else {}),
                )
                upload_url = str(_response_value(response, "upload_url") or "")
                file_id = str(_response_value(response, "file_id") or "")
                _check_upload_url(upload_url)
                if not re.fullmatch(r"F[A-Z0-9]+", file_id):
                    raise SlackError("Slack returned an invalid external upload file reference.")
                entry.update(file_id=file_id, stage="url_allocated")
                self._write_outbound_state(intent, state)
                self._binary_uploader(upload_url, data, attachment.mime_type)
                entry["stage"] = "uploaded"
                self._write_outbound_state(intent, state)

            file_payload = [
                {"id": files_state[value.id]["file_id"], "title": value.filename}
                for value in intent.attachments
            ]
            state["status"] = "completing"
            self._write_outbound_state(intent, state)
            chunks = slack_message_chunks(intent.text) if intent.text else [""]
            kwargs: dict[str, Any] = {
                "files": file_payload,
                "channel_id": conversation,
            }
            if chunks[0]:
                kwargs["initial_comment"] = chunks[0]
            if intent.thread_id is not None:
                kwargs["thread_ts"] = str(intent.thread_id)
            response = self._api_call(
                "files.completeUploadExternal",
                self._web.files_completeUploadExternal,
                **kwargs,
            )
            file_ids = tuple(value["id"] for value in file_payload)
            state.update(
                status="completing",
                attachment_references=list(file_ids),
                provider_reference=f"slack-files:{','.join(file_ids)}",
            )
            self._write_outbound_state(intent, state)
            return self._finish_attachment_delivery(
                intent,
                state,
                chunks,
                message_id=_response_value(response, "ts") or None,
            )
        except SlackError as error:
            message = str(error)
            if state.get("attachment_references"):
                if attempts >= 3:
                    return self._partial_attachment_fallback(intent, state)
                raise NotificationDeliveryError(
                    "Slack shared the attachments but a trailing text chunk failed temporarily.",
                    retryable=True,
                    ambiguous=False,
                ) from error
            permission = "missing_scope" in message or "not_allowed_token_type" in message
            if permission:
                return self._attachment_fallback(
                    intent,
                    "Attachment not sent because the Slack app lacks files:write; add the scope and reinstall the app.",
                )
            if attempts >= 3 or _permanent_upload_error(message):
                return self._attachment_fallback(
                    intent,
                    "Attachment upload failed after validation; the text reply was preserved.",
                )
            raise NotificationDeliveryError(
                "Slack attachment upload failed temporarily.",
                retryable=True,
                ambiguous=False,
            ) from error
        except (OSError, ValueError) as error:
            if attempts >= 3 or isinstance(error, ValueError):
                return self._attachment_fallback(
                    intent,
                    "Attachment did not pass the Slack provider safety checks; the text reply was preserved.",
                )
            raise NotificationDeliveryError(
                "Slack attachment upload failed temporarily.", retryable=True,
            ) from error

    def _finish_attachment_delivery(
        self,
        intent: NotificationIntent,
        state: dict[str, Any],
        chunks: list[str],
        *,
        message_id: MessageId | None = None,
    ) -> NotificationReceipt:
        sent = {
            int(value)
            for value in state.get("text_chunks_sent", ())
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
        }
        for index, chunk in enumerate(chunks[1:], 1):
            if index in sent:
                continue
            trailing = NotificationIntent(
                idempotency_key=f"{intent.idempotency_key}:text:{index}",
                operation="send",
                conversation_id=intent.conversation_id,
                text=chunk,
                thread_id=intent.thread_id,
            )
            self._post_notification_text(trailing)
            sent.add(index)
            state["text_chunks_sent"] = sorted(sent)
            self._write_outbound_state(intent, state)
        state["status"] = "delivered"
        self._write_outbound_state(intent, state)
        receipt = _state_receipt(intent, state)
        return NotificationReceipt(
            idempotency_key=receipt.idempotency_key,
            status=receipt.status,
            message_id=message_id,
            provider_reference=receipt.provider_reference,
            attachment_references=receipt.attachment_references,
        )

    def _partial_attachment_fallback(
        self,
        intent: NotificationIntent,
        state: dict[str, Any],
    ) -> NotificationReceipt:
        file_ids = _state_file_ids(state)
        fallback = NotificationIntent(
            idempotency_key=f"{intent.idempotency_key}:partial",
            operation="send",
            conversation_id=intent.conversation_id,
            text="[Some attachments could not be confirmed after an interrupted Slack upload.]",
            thread_id=intent.thread_id,
        )
        self._post_notification_text(fallback)
        state.update(
            status="delivered",
            attachment_references=list(file_ids),
            provider_reference=f"slack-files-partial:{','.join(file_ids)}",
        )
        self._write_outbound_state(intent, state)
        return _state_receipt(intent, state)

    def _attachment_fallback(self, intent: NotificationIntent, note: str) -> NotificationReceipt:
        fallback = NotificationIntent(
            idempotency_key=f"{intent.idempotency_key}:fallback",
            operation="send",
            conversation_id=intent.conversation_id,
            text="\n\n".join(value for value in (intent.text, f"[{note}]") if value),
            thread_id=intent.thread_id,
        )
        message_id = self._post_notification_text(fallback)
        state = self._load_outbound_state(intent)
        state.update(status="delivered", provider_reference="slack:text-fallback")
        self._write_outbound_state(intent, state)
        return NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="delivered",
            message_id=message_id,
            provider_reference="slack:text-fallback",
            detail=note,
        )

    def _verified_artifact(self, attachment: OutboundAttachment) -> bytes:
        relative = _artifact_relative(attachment.uri)
        attachment_suffix = Path(attachment.filename).suffix.lower()
        attachment_stem = Path(attachment.filename).stem.lower()
        if attachment.filename.startswith(".") or attachment_suffix in {
            ".key", ".pem", ".p12", ".pfx", ".kdb", ".keystore",
        } or attachment_stem in {
            "secret", "secrets", "credential", "credentials", "token", "tokens",
        }:
            raise ValueError("Slack attachment filename is sensitive.")
        for root in self._approved_artifact_roots:
            candidate = root / relative
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if root not in resolved.parents:
                continue
            if any((root.joinpath(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
                raise ValueError("Slack attachment path contains a symbolic link.")
            info = resolved.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != attachment.size
            ):
                raise ValueError("Slack attachment size or file type changed before upload.")
            if info.st_size > self.config.max_upload_bytes:
                raise ValueError("Slack attachment exceeds the configured upload limit.")
            data = _read_verified_file(resolved, self.config.max_upload_bytes)
            if hashlib.sha256(data).hexdigest() != attachment.sha256:
                raise ValueError("Slack attachment digest changed before upload.")
            if attachment_suffix != resolved.suffix.lower():
                raise ValueError("Slack attachment extension does not match its artifact.")
            if _detected_mime(attachment_suffix, data) != attachment.mime_type:
                raise ValueError("Slack attachment MIME type does not match its content.")
            if _looks_sensitive_content(data):
                raise ValueError("Slack attachment content is sensitive.")
            return data
        raise ValueError("Slack attachment is outside approved artifact roots.")

    def _load_outbound_state(self, intent: NotificationIntent) -> dict[str, Any]:
        path = self._outbound_state_path(intent.idempotency_key)
        fingerprint = _intent_fingerprint(intent)
        if not path.is_file():
            return {"schema_version": 1, "fingerprint": fingerprint, "status": "pending", "attempts": 0, "files": {}}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SlackError("Slack outbound delivery state is unreadable.") from error
        if not isinstance(state, dict) or state.get("fingerprint") != fingerprint:
            raise SlackError("Slack outbound idempotency key was reused for another intent.")
        return state

    def _write_outbound_state(self, intent: NotificationIntent, state: dict[str, Any]) -> None:
        state["schema_version"] = 1
        state["fingerprint"] = _intent_fingerprint(intent)
        _atomic_json(self._outbound_state_path(intent.idempotency_key), state)

    def _outbound_state_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._outbound_state_dir / f"delivery-{digest}.json"

    def _slack_file_shared(self, file_id: str, conversation: str) -> bool:
        response = self._api_call("files.info", self._web.files_info, file=file_id)
        info = _response_value(response, "file", {})
        if not isinstance(info, dict):
            return False
        shares = info.get("shares")
        if not isinstance(shares, dict):
            return False
        for visibility in shares.values():
            if isinstance(visibility, dict) and conversation in visibility:
                return True
        return False

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

    def download_attachment(
        self, attachment: Attachment, destination: Path, *, max_bytes: int,
    ) -> None:
        """Resolve the file through Slack; never authenticate event-supplied URLs."""
        if max_bytes < 1 or attachment.size > max_bytes:
            raise SlackError("Slack attachment exceeds the download size limit.")
        file_id = attachment.file_id
        if not re.fullmatch(r"F[A-Z0-9]+", file_id):
            raise SlackError("Slack attachment has no valid file ID.")
        response = self._api_call("files.info", self._web.files_info, file=file_id)
        info = _response_value(response, "file", {})
        if not isinstance(info, dict) or info.get("id") != file_id:
            raise SlackError("Slack did not return the requested file.")
        if info.get("is_external"):
            raise SlackError("External Slack files must be opened through their original service.")
        if _file_size(info.get("size")) > max_bytes:
            raise SlackError("Slack attachment exceeds the download size limit.")
        url = str(info.get("url_private_download") or info.get("url_private") or "")
        _check_download_url(url)
        request = Request(url, headers={"Authorization": f"Bearer {self.config.bot_token}"})
        try:
            with build_opener(_SlackRedirectHandler()).open(request, timeout=30) as source:
                _check_download_url(source.geturl())
                if _file_size(source.headers.get("Content-Length")) > max_bytes:
                    raise SlackError("Slack attachment exceeds the download size limit.")
                with destination.open("wb") as output:
                    os.chmod(destination, 0o600)
                    total = 0
                    while chunk := source.read(min(64 * 1024, max_bytes - total + 1)):
                        total += len(chunk)
                        if total > max_bytes:
                            raise SlackError("Slack attachment exceeds the download size limit.")
                        output.write(chunk)
                    if not total:
                        raise SlackError("Slack returned an empty attachment.")
        except Exception as error:
            destination.unlink(missing_ok=True)
            if isinstance(error, SlackError):
                raise
            raise SlackError("Slack file download failed; check files:read permission and file access.") from None

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
            needed = _response_value(response, "needed")
            if needed:
                detail = f"{detail} (needed: {needed})"
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
    attachments = _file_attachments(native.get("files"))
    if not text and not attachments:
        return None
    text = _translate_secondary_command(text)
    message_id = _optional_id(native.get("ts"))
    thread_id = _optional_id(native.get("thread_ts"))
    return ChatEvent(
        cursor=cursor,
        conversation_id=conversation,
        message_id=message_id,
        text=text,
        raw=deepcopy(payload),
        attachments=attachments,
        thread_id=thread_id,
    )


def _file_size(value: object) -> int:
    try:
        return max(0, int(value)) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _file_attachments(files: object) -> tuple[Attachment, ...]:
    if not isinstance(files, list):
        return ()
    result = []
    for info in files:
        if not isinstance(info, dict):
            continue
        file_id = str(info.get("id") or "")
        if not re.fullmatch(r"F[A-Z0-9]+", file_id):
            continue
        mime = str(info.get("mimetype") or "").lower()
        result.append(Attachment(
            kind="image" if mime in {"image/jpeg", "image/png", "image/webp", "image/gif"} else "document",
            file_id=file_id, mime_type=mime,
            filename=str(info.get("name") or info.get("title") or file_id),
            size=_file_size(info.get("size")),
        ))
    return tuple(result)


def _check_download_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == "files.slack.com"
                 and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise SlackError("Slack returned an unsupported file download location.")


def _check_upload_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "files.slack.com"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SlackError("Slack returned an unsupported external upload location.")


def _upload_binary(url: str, data: bytes, mime_type: str) -> None:
    _check_upload_url(url)
    request = Request(
        url,
        data=data,
        headers={"Content-Type": mime_type, "Content-Length": str(len(data))},
        method="POST",
    )
    try:
        with build_opener(_SlackRedirectHandler()).open(request, timeout=30) as response:
            _check_upload_url(response.geturl())
            if int(getattr(response, "status", 200)) not in {200, 201, 204}:
                raise SlackError("Slack external binary upload returned an unsuccessful status.")
    except SlackError:
        raise
    except Exception as error:
        raise SlackError("Slack external binary upload failed.") from error


class _SlackRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib normally forwards Authorization on redirects, including off-host.
        _check_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
    command = str(payload.get("command") or "").strip().lower()
    if not _SLASH_COMMAND.fullmatch(command):
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


def _translate_secondary_command(text: str) -> str:
    match = _SECONDARY_COMMAND.fullmatch(text)
    if match is None:
        return text
    command, argument = match.groups()
    suffix = f" {argument}" if argument else ""
    return f"/{command}{suffix}"


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
    needed = _response_value(response, "needed") if response is not None else ""
    if detail and needed:
        detail = f"{detail} (needed: {needed})"
    return str(detail or error or type(error).__name__)


def _artifact_relative(uri: str) -> Path:
    if not uri.startswith("artifact://"):
        raise ValueError("Slack attachments must use artifact URIs.")
    value = uri.removeprefix("artifact://")
    parts = tuple(part for part in value.split("/") if part)
    if not parts or value.startswith("/") or any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("Slack attachment artifact URI is invalid.")
    return Path(*parts)


def _read_verified_file(path: Path, max_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 1
            or info.st_size > max_bytes
        ):
            raise ValueError("Slack attachment size or file type is invalid.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        if len(data) != info.st_size or len(data) > max_bytes:
            raise ValueError("Slack attachment changed while it was being validated.")
        return data
    finally:
        os.close(descriptor)


def _looks_sensitive_content(data: bytes) -> bool:
    sample = data[:262_144]
    return any(
        marker in sample
        for marker in (
            b"-----BEGIN PRIVATE KEY-----",
            b"-----BEGIN RSA PRIVATE KEY-----",
            b"-----BEGIN EC PRIVATE KEY-----",
            b"-----BEGIN OPENSSH PRIVATE KEY-----",
            b"xoxb-",
            b"xapp-",
            b"ghp_",
        )
    )


def _detected_mime(suffix: str, data: bytes) -> str:
    if (
        suffix == ".png"
        and data.startswith(b"\x89PNG\r\n\x1a\n")
        and data.endswith(b"IEND\xaeB`\x82")
    ):
        return "image/png"
    if suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if suffix == ".webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if suffix == ".gif" and data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";"):
        return "image/gif"
    if suffix == ".pdf" and data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]:
        return "application/pdf"
    office = {
        ".docx": ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ".xlsx": ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ".pptx": ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    }
    if suffix in office:
        marker, mime = office[suffix]
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
                if "[Content_Types].xml" in names and any(name.startswith(marker) for name in names):
                    return mime
        except (OSError, zipfile.BadZipFile):
            pass
        return ""
    text_types = {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".json": "application/json",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }
    if suffix in text_types and b"\x00" not in data:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        if suffix == ".json":
            try:
                json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ""
        return text_types[suffix]
    return ""


def _intent_fingerprint(intent: NotificationIntent) -> str:
    payload = {
        "operation": intent.operation,
        "conversation_id": intent.conversation_id,
        "thread_id": intent.thread_id,
        "text": intent.text,
        "attachments": [
            {
                "id": value.id,
                "uri": value.uri,
                "filename": value.filename,
                "mime_type": value.mime_type,
                "size": value.size,
                "sha256": value.sha256,
            }
            for value in intent.attachments
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _state_file_ids(state: dict[str, Any]) -> tuple[str, ...]:
    files = state.get("files")
    if not isinstance(files, dict):
        return ()
    return tuple(
        str(value.get("file_id"))
        for value in files.values()
        if isinstance(value, dict) and re.fullmatch(r"F[A-Z0-9]+", str(value.get("file_id") or ""))
    )


def _state_receipt(intent: NotificationIntent, state: dict[str, Any]) -> NotificationReceipt:
    references = tuple(str(value) for value in state.get("attachment_references", ()) if str(value))
    return NotificationReceipt(
        idempotency_key=intent.idempotency_key,
        status="delivered",
        provider_reference=str(state.get("provider_reference") or "slack-files:" + ",".join(references)),
        attachment_references=references,
    )


def _permanent_upload_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "invalid_arguments",
            "invalid_auth",
            "account_inactive",
            "file_uploads_disabled",
            "unsupported external upload location",
            "invalid external upload file reference",
        )
    )


def _create_web_client(config: SlackConfig) -> Any:
    try:
        from slack_sdk.web import WebClient
    except ImportError as error:
        raise SlackError(
            "Install our-ark-slack with its slack-sdk dependency."
        ) from error
    return WebClient(token=config.bot_token, timeout=config.receive_timeout)
