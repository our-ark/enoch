from __future__ import annotations

from typing import Protocol, runtime_checkable

ConversationId = int | str
MessageId = int | str


@runtime_checkable
class MessagePinProvider(Protocol):
    def pin_message(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
    ) -> None: ...
