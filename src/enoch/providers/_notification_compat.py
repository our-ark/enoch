from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

NOTIFICATION_CONTRACT_VERSION = 2
ConversationId = int | str
MessageId = int | str


def _normalize_message_id(value: object) -> MessageId | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value != 0 else None
    if isinstance(value, str):
        return value.strip() or None
    return None


class NotificationDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class NotificationCapabilities:
    idempotent_delivery: bool = False
    reconciliation: bool = False
    attachments: bool = False
    contract_version: int = NOTIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != NOTIFICATION_CONTRACT_VERSION:
            raise ValueError("Unsupported notification contract version.")


@dataclass(frozen=True)
class NotificationIntent:
    idempotency_key: str
    operation: str
    conversation_id: ConversationId
    text: str
    message_id: MessageId | None = None
    thread_id: MessageId | None = None
    attachments: tuple[Any, ...] = ()
    daemon_epoch: str = ""
    contract_version: int = NOTIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        key = self.idempotency_key.strip()
        operation = self.operation.strip().lower()
        if self.contract_version != NOTIFICATION_CONTRACT_VERSION:
            raise ValueError("Unsupported notification contract version.")
        if not key:
            raise ValueError("Notification idempotency key is required.")
        if operation not in {"send", "edit"}:
            raise ValueError("Notification operation must be send or edit.")
        if operation == "edit" and _normalize_message_id(self.message_id) is None:
            raise ValueError("Edit notifications require a message id.")
        if operation == "edit" and self.attachments:
            raise ValueError("Edit notifications cannot add attachments.")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "text", str(self.text))
        object.__setattr__(self, "thread_id", _normalize_message_id(self.thread_id))
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "daemon_epoch", self.daemon_epoch.strip())


@dataclass(frozen=True)
class NotificationReceipt:
    idempotency_key: str
    status: str
    message_id: MessageId | None = None
    provider_reference: str = ""
    attachment_references: tuple[str, ...] = ()
    detail: str = ""
    contract_version: int = NOTIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        key = self.idempotency_key.strip()
        status = self.status.strip().lower()
        if not key:
            raise ValueError("Notification receipt idempotency key is required.")
        if self.contract_version != NOTIFICATION_CONTRACT_VERSION:
            raise ValueError("Unsupported notification contract version.")
        if status not in {"delivered", "not_found", "unknown"}:
            raise ValueError(
                "Notification receipt status must be delivered, not_found, or unknown."
            )
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "provider_reference", self.provider_reference.strip())
        object.__setattr__(
            self,
            "attachment_references",
            tuple(str(value).strip() for value in self.attachment_references if str(value).strip()),
        )
        object.__setattr__(self, "detail", self.detail.strip())


@runtime_checkable
class DurableNotificationProvider(Protocol):
    @property
    def notification_capabilities(self) -> NotificationCapabilities: ...

    def deliver_notification(
        self,
        intent: NotificationIntent,
    ) -> NotificationReceipt: ...

    def reconcile_notification(
        self,
        intent: NotificationIntent,
    ) -> NotificationReceipt: ...
