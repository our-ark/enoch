from __future__ import annotations

from typing import Protocol, runtime_checkable

ConversationId = int | str
MessageId = int | str


@runtime_checkable
class MessageDeleteProvider(Protocol):
    def delete_message(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
    ) -> None: ...


@runtime_checkable
class SilentMessageProvider(Protocol):
    def send_silent_message(
        self,
        conversation_id: ConversationId,
        text: str,
    ) -> MessageId | None: ...
