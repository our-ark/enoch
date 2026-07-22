"""Enoch adapter for the shared provider contract library."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from enoch.runtime_dependencies import activate_runtime_dependencies


activate_runtime_dependencies()

from our_ark_provider_kit import (  # noqa: E402
    AgentIdentity,
    AgentRuntime,
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    Attachment,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ConversationId,
    ForgeProvider,
    ForgeProviderError,
    MessageId,
    ProgressCallback,
    ProviderHealth,
    VersionControlProvider,
    VersionControlProviderError,
    normalize_conversation_id,
    normalize_message_id,
)

try:  # Remove after the provider-kit dependency pin includes channel/v2.
    from our_ark_provider_kit import AttachmentProvider, Cursor  # noqa: E402
except ImportError:
    Cursor = int | str

    @runtime_checkable
    class AttachmentProvider(Protocol):
        def download_attachment(
            self,
            attachment: Attachment,
            destination: Path,
            *,
            max_bytes: int,
        ) -> None: ...


__all__ = [
    "AgentIdentity",
    "AgentRuntime",
    "AgentRuntimeAccessUnavailable",
    "AgentRuntimeCancelled",
    "AgentRuntimeError",
    "Attachment",
    "AttachmentProvider",
    "ChatEvent",
    "ChatProvider",
    "ChatProviderError",
    "ConversationId",
    "Cursor",
    "ForgeProvider",
    "ForgeProviderError",
    "MessageId",
    "ProgressCallback",
    "ProviderHealth",
    "VersionControlProvider",
    "VersionControlProviderError",
    "normalize_conversation_id",
    "normalize_message_id",
]
