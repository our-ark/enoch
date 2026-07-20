"""Enoch adapter for the shared provider contract library."""

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


__all__ = [
    "AgentIdentity",
    "AgentRuntime",
    "AgentRuntimeAccessUnavailable",
    "AgentRuntimeCancelled",
    "AgentRuntimeError",
    "Attachment",
    "ChatEvent",
    "ChatProvider",
    "ChatProviderError",
    "ConversationId",
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
