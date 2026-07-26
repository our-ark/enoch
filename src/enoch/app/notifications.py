from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any

from enoch.app.epoch import DaemonEpoch, daemon_epoch_guard
from enoch.memory.paths import now as current_time
from enoch.paths import enoch_home
from enoch.providers.contracts import (
    ChatProvider,
    ChatProviderError,
    ConversationId,
    DurableNotificationProvider,
    MessageId,
    NotificationCapabilities,
    NotificationDeliveryError,
    NotificationIntent,
    NotificationReceipt,
    normalize_conversation_id,
    normalize_message_id,
)
from enoch.providers.authorization import CapabilityAuthorizer
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object


SCHEMA_VERSION = 1
MAX_DELIVERY_ATTEMPTS = 3
PENDING = "pending"
IN_FLIGHT = "in_flight"
DELIVERED = "delivered"
RETRYABLE_FAILURE = "retryable_failure"
TERMINAL_FAILURE = "terminal_failure"
NOTIFICATION_STATUSES = {
    PENDING,
    IN_FLIGHT,
    DELIVERED,
    RETRYABLE_FAILURE,
    TERMINAL_FAILURE,
}


@dataclass(frozen=True)
class NotificationRecord:
    idempotency_key: str
    operation: str
    conversation_id: ConversationId
    text: str
    status: str
    message_id: MessageId | None = None
    receipt_message_id: MessageId | None = None
    provider_reference: str = ""
    owner_epoch: str = ""
    attempts: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class NotificationResult:
    delivered: bool
    message_id: MessageId | None = None
    error: str = ""
    terminal: bool = False
    record: NotificationRecord | None = None


class NotificationDeliveryService:
    """Durable, fenced send/edit delivery for one chat provider."""

    def __init__(
        self,
        provider: ChatProvider,
        provider_name: str,
        root: Path,
        epoch: DaemonEpoch,
        authorizer: CapabilityAuthorizer | None = None,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name.strip().lower() or "chat"
        self.root = root
        self.epoch = epoch
        self.authorizer = authorizer

    def send(
        self,
        conversation_id: ConversationId,
        text: str,
        *,
        idempotency_key: str,
    ) -> NotificationResult:
        self._authorize("notification.send", ("chat.send",))
        intent = NotificationIntent(
            idempotency_key=idempotency_key,
            operation="send",
            conversation_id=conversation_id,
            text=text,
            daemon_epoch=self.epoch.token,
        )
        return self._deliver(intent)

    def edit(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
        text: str,
        *,
        idempotency_key: str,
    ) -> NotificationResult:
        self._authorize("notification.edit", ("chat.edit",))
        intent = NotificationIntent(
            idempotency_key=idempotency_key,
            operation="edit",
            conversation_id=conversation_id,
            message_id=message_id,
            text=text,
            daemon_epoch=self.epoch.token,
        )
        return self._deliver(intent)

    def recover(self) -> tuple[NotificationResult, ...]:
        with daemon_epoch_guard(self.epoch, self.root):
            records = tuple(
                record
                for record in notification_records(self.provider_name, self.root)
                if record.status in {PENDING, IN_FLIGHT, RETRYABLE_FAILURE}
            )
            results: list[NotificationResult] = []
            for record in records:
                self._authorize(
                    f"notification.recover-{record.operation}",
                    (f"chat.{record.operation}",),
                )
                if record.status == IN_FLIGHT:
                    results.append(self._recover_in_flight(record))
                    continue
                intent = _record_intent(record, self.epoch)
                claimed = reclaim_notification(
                    self.provider_name,
                    record.idempotency_key,
                    self.epoch,
                    self.root,
                )
                results.append(self._invoke_provider(intent, claimed))
            return tuple(results)

    def _deliver(self, intent: NotificationIntent) -> NotificationResult:
        with daemon_epoch_guard(self.epoch, self.root):
            existing = notification_record(
                self.provider_name,
                intent.idempotency_key,
                self.root,
            )
            if existing is None:
                existing = persist_notification_intent(
                    self.provider_name,
                    intent,
                    self.root,
                )
            else:
                mismatch = _intent_mismatch(existing, intent)
                if mismatch:
                    return NotificationResult(
                        delivered=False,
                        error=mismatch,
                        terminal=True,
                        record=existing,
                    )
                if existing.status == DELIVERED:
                    return _result(existing)
                if existing.status == TERMINAL_FAILURE:
                    return _result(existing)
                if existing.status == IN_FLIGHT:
                    if existing.owner_epoch == self.epoch.token:
                        return NotificationResult(
                            delivered=False,
                            error="Notification delivery is already in flight.",
                            terminal=False,
                            record=existing,
                        )
                    recovered = self._recover_in_flight(existing)
                    if recovered.delivered or recovered.terminal:
                        return recovered

            claimed = reclaim_notification(
                self.provider_name,
                intent.idempotency_key,
                self.epoch,
                self.root,
            )
            return self._invoke_provider(intent, claimed)

    def _recover_in_flight(self, record: NotificationRecord) -> NotificationResult:
        intent = _record_intent(record, self.epoch)
        durable = _durable_provider(self.provider)
        if durable is None:
            failed = fail_notification(
                self.provider_name,
                record.idempotency_key,
                (
                    "Delivery outcome is ambiguous after restart and this provider "
                    "cannot reconcile or replay idempotently; notification was not resent."
                ),
                self.root,
                terminal=True,
            )
            return _result(failed)

        capabilities = durable.notification_capabilities
        if capabilities.reconciliation:
            try:
                receipt = durable.reconcile_notification(intent)
            except (OSError, ChatProviderError) as error:
                failed = fail_notification(
                    self.provider_name,
                    record.idempotency_key,
                    f"Notification reconciliation failed: {error}",
                    self.root,
                    terminal=not capabilities.idempotent_delivery,
                )
                return _result(failed)
            if receipt.status == "delivered":
                completed = complete_notification(
                    self.provider_name,
                    record.idempotency_key,
                    receipt,
                    self.root,
                )
                return _result(completed)
            if receipt.status == "not_found":
                if record.attempts >= MAX_DELIVERY_ATTEMPTS:
                    failed = fail_notification(
                        self.provider_name,
                        record.idempotency_key,
                        "Notification delivery attempts were exhausted during recovery.",
                        self.root,
                        terminal=True,
                    )
                    return _result(failed)
                claimed = reclaim_notification(
                    self.provider_name,
                    record.idempotency_key,
                    self.epoch,
                    self.root,
                )
                return self._invoke_provider(intent, claimed)
            if not capabilities.idempotent_delivery:
                failed = fail_notification(
                    self.provider_name,
                    record.idempotency_key,
                    "Provider reconciliation returned an unknown delivery outcome.",
                    self.root,
                    terminal=True,
                )
                return _result(failed)

        if capabilities.idempotent_delivery:
            if record.attempts >= MAX_DELIVERY_ATTEMPTS:
                failed = fail_notification(
                    self.provider_name,
                    record.idempotency_key,
                    "Notification delivery attempts were exhausted during recovery.",
                    self.root,
                    terminal=True,
                )
                return _result(failed)
            claimed = reclaim_notification(
                self.provider_name,
                record.idempotency_key,
                self.epoch,
                self.root,
            )
            return self._invoke_provider(intent, claimed)

        failed = fail_notification(
            self.provider_name,
            record.idempotency_key,
            "Provider cannot safely recover an ambiguous delivery.",
            self.root,
            terminal=True,
        )
        return _result(failed)

    def _invoke_provider(
        self,
        intent: NotificationIntent,
        claimed: NotificationRecord,
    ) -> NotificationResult:
        try:
            receipt = self._provider_delivery(intent)
        except NotificationDeliveryError as error:
            capabilities = _capabilities(self.provider)
            terminal = (
                (error.ambiguous and not capabilities.idempotent_delivery)
                or not error.retryable
                or claimed.attempts >= MAX_DELIVERY_ATTEMPTS
            )
            failed = fail_notification(
                self.provider_name,
                intent.idempotency_key,
                str(error),
                self.root,
                terminal=terminal,
            )
            return _result(failed)
        except (OSError, ChatProviderError) as error:
            failed = fail_notification(
                self.provider_name,
                intent.idempotency_key,
                str(error),
                self.root,
                terminal=claimed.attempts >= MAX_DELIVERY_ATTEMPTS,
            )
            return _result(failed)

        receipt_error = _receipt_error(intent, receipt)
        if receipt_error:
            failed = fail_notification(
                self.provider_name,
                intent.idempotency_key,
                receipt_error,
                self.root,
                terminal=True,
            )
            return _result(failed)
        if receipt.status != "delivered":
            terminal = (
                receipt.status == "unknown"
                and not _capabilities(self.provider).idempotent_delivery
            )
            failed = fail_notification(
                self.provider_name,
                intent.idempotency_key,
                receipt.detail or f"Provider returned {receipt.status}.",
                self.root,
                terminal=terminal,
            )
            return _result(failed)

        completed = complete_notification(
            self.provider_name,
            intent.idempotency_key,
            receipt,
            self.root,
        )
        return _result(completed)

    def _provider_delivery(self, intent: NotificationIntent) -> NotificationReceipt:
        durable = _durable_provider(self.provider)
        if durable is not None:
            return durable.deliver_notification(intent)
        if intent.operation == "send":
            message_id = self.provider.send_message(intent.conversation_id, intent.text)
        else:
            assert intent.message_id is not None
            self.provider.edit_message(
                intent.conversation_id,
                intent.message_id,
                intent.text,
            )
            message_id = intent.message_id
        return NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="delivered",
            message_id=message_id,
        )

    def _authorize(self, action: str, requirements: tuple[str, ...]) -> None:
        if self.authorizer is not None:
            self.authorizer.require(action, requirements)


def notifications_path(provider: str, root: Path | None = None) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in provider.strip().lower()
    ).strip("-.") or "chat"
    return enoch_home(root) / "channels" / safe / "notifications.json"


def notification_records(
    provider: str,
    root: Path | None = None,
) -> tuple[NotificationRecord, ...]:
    path = notifications_path(provider, root)
    with file_transaction(path):
        data = _load_notifications(path)
        return tuple(_parse_record(key, raw) for key, raw in data["notifications"].items())


def notification_record(
    provider: str,
    idempotency_key: str,
    root: Path | None = None,
) -> NotificationRecord | None:
    path = notifications_path(provider, root)
    with file_transaction(path):
        data = _load_notifications(path)
        raw = data["notifications"].get(idempotency_key)
        return _parse_record(idempotency_key, raw) if raw is not None else None


def persist_notification_intent(
    provider: str,
    intent: NotificationIntent,
    root: Path | None = None,
) -> NotificationRecord:
    path = notifications_path(provider, root)
    with file_transaction(path):
        data = _load_notifications(path)
        existing_raw = data["notifications"].get(intent.idempotency_key)
        if existing_raw is not None:
            existing = _parse_record(intent.idempotency_key, existing_raw)
            mismatch = _intent_mismatch(existing, intent)
            if mismatch:
                raise ValueError(mismatch)
            return existing
        timestamp = current_time()
        record = NotificationRecord(
            idempotency_key=intent.idempotency_key,
            operation=intent.operation,
            conversation_id=intent.conversation_id,
            text=intent.text,
            message_id=intent.message_id,
            status=PENDING,
            owner_epoch="",
            attempts=0,
            created_at=timestamp,
            updated_at=timestamp,
        )
        data["notifications"][record.idempotency_key] = _record_json(record)
        _write_notifications(path, data)
        return record


def claim_notification(
    provider: str,
    intent: NotificationIntent,
    epoch: DaemonEpoch,
    root: Path | None = None,
) -> NotificationRecord:
    persist_notification_intent(provider, intent, root)
    return reclaim_notification(provider, intent.idempotency_key, epoch, root)


def reclaim_notification(
    provider: str,
    idempotency_key: str,
    epoch: DaemonEpoch,
    root: Path | None = None,
) -> NotificationRecord:
    path = notifications_path(provider, root)
    with file_transaction(path):
        data = _load_notifications(path)
        existing = _parse_record(
            idempotency_key,
            data["notifications"].get(idempotency_key),
        )
        record = replace(
            existing,
            status=IN_FLIGHT,
            owner_epoch=epoch.token,
            attempts=existing.attempts + 1,
            error="",
            updated_at=current_time(),
        )
        data["notifications"][idempotency_key] = _record_json(record)
        _write_notifications(path, data)
        return record


def complete_notification(
    provider: str,
    idempotency_key: str,
    receipt: NotificationReceipt,
    root: Path | None = None,
) -> NotificationRecord:
    return _update_notification(
        provider,
        idempotency_key,
        root,
        status=DELIVERED,
        receipt_message_id=receipt.message_id,
        provider_reference=receipt.provider_reference,
        error="",
    )


def fail_notification(
    provider: str,
    idempotency_key: str,
    error: str,
    root: Path | None = None,
    *,
    terminal: bool,
) -> NotificationRecord:
    return _update_notification(
        provider,
        idempotency_key,
        root,
        status=TERMINAL_FAILURE if terminal else RETRYABLE_FAILURE,
        error=" ".join(error.split())[:2000],
    )


def _update_notification(
    provider: str,
    idempotency_key: str,
    root: Path | None,
    **changes: Any,
) -> NotificationRecord:
    path = notifications_path(provider, root)
    with file_transaction(path):
        data = _load_notifications(path)
        existing = _parse_record(
            idempotency_key,
            data["notifications"].get(idempotency_key),
        )
        record = replace(existing, updated_at=current_time(), **changes)
        data["notifications"][idempotency_key] = _record_json(record)
        _write_notifications(path, data)
        return record


def _load_notifications(path: Path) -> dict[str, Any]:
    data = load_json_object(
        path,
        default_factory=lambda: {
            "schema_version": SCHEMA_VERSION,
            "notifications": {},
        },
    )
    schema = data.get("schema_version", SCHEMA_VERSION)
    notifications = data.get("notifications")
    if schema != SCHEMA_VERSION:
        raise StateCorruptionError(path, f"unsupported schema version {schema}")
    if not isinstance(notifications, dict):
        raise StateCorruptionError(path, "expected notifications to be an object")
    for key, raw in notifications.items():
        if not isinstance(key, str):
            raise StateCorruptionError(path, "found a non-string notification key")
        _parse_record(key, raw)
    return {"schema_version": SCHEMA_VERSION, "notifications": notifications}


def _write_notifications(path: Path, data: dict[str, Any]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": current_time(),
        "notifications": data["notifications"],
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _parse_record(key: str, raw: object) -> NotificationRecord:
    if not isinstance(raw, dict):
        raise StateCorruptionError(Path("<notification>"), f"invalid record {key}")
    operation = str(raw.get("operation") or "").strip().lower()
    status = str(raw.get("status") or "").strip().lower()
    conversation_id = normalize_conversation_id(raw.get("conversation_id"))
    message_id = normalize_message_id(raw.get("message_id"))
    receipt_message_id = normalize_message_id(raw.get("receipt_message_id"))
    text = str(raw.get("text") or "")
    if (
        not key.strip()
        or operation not in {"send", "edit"}
        or status not in NOTIFICATION_STATUSES
        or conversation_id is None
        or (operation == "edit" and message_id is None)
    ):
        raise StateCorruptionError(Path("<notification>"), f"invalid record {key}")
    return NotificationRecord(
        idempotency_key=key,
        operation=operation,
        conversation_id=conversation_id,
        text=text,
        status=status,
        message_id=message_id,
        receipt_message_id=receipt_message_id,
        provider_reference=str(raw.get("provider_reference") or ""),
        owner_epoch=str(raw.get("owner_epoch") or ""),
        attempts=max(0, _int(raw.get("attempts"))),
        error=str(raw.get("error") or ""),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _record_json(record: NotificationRecord) -> dict[str, Any]:
    return {
        "operation": record.operation,
        "conversation_id": record.conversation_id,
        "text": record.text,
        "status": record.status,
        "message_id": record.message_id,
        "receipt_message_id": record.receipt_message_id,
        "provider_reference": record.provider_reference,
        "owner_epoch": record.owner_epoch,
        "attempts": record.attempts,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _record_intent(
    record: NotificationRecord,
    epoch: DaemonEpoch,
) -> NotificationIntent:
    return NotificationIntent(
        idempotency_key=record.idempotency_key,
        operation=record.operation,
        conversation_id=record.conversation_id,
        text=record.text,
        message_id=record.message_id,
        daemon_epoch=epoch.token,
    )


def _intent_mismatch(
    record: NotificationRecord,
    intent: NotificationIntent,
) -> str:
    if (
        record.operation != intent.operation
        or record.conversation_id != intent.conversation_id
        or record.message_id != intent.message_id
        or record.text != intent.text
    ):
        return (
            f"Notification key {intent.idempotency_key} was reused for a different intent."
        )
    return ""


def _receipt_error(
    intent: NotificationIntent,
    receipt: NotificationReceipt,
) -> str:
    if receipt.idempotency_key != intent.idempotency_key:
        return (
            "Provider returned a receipt for a different notification "
            f"({receipt.idempotency_key or 'missing key'})."
        )
    return ""


def _durable_provider(
    provider: ChatProvider,
) -> DurableNotificationProvider | None:
    return provider if isinstance(provider, DurableNotificationProvider) else None


def _capabilities(provider: ChatProvider) -> NotificationCapabilities:
    durable = _durable_provider(provider)
    return (
        durable.notification_capabilities
        if durable is not None
        else NotificationCapabilities()
    )


def _result(record: NotificationRecord) -> NotificationResult:
    return NotificationResult(
        delivered=record.status == DELIVERED,
        message_id=record.receipt_message_id or record.message_id,
        error=record.error,
        terminal=record.status == TERMINAL_FAILURE,
        record=record,
    )


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
