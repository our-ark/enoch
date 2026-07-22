"""Agent-body adapter for the shared provider contract library."""

import os


os.environ["OUR_ARK_AGENT_PACKAGE"] = __package__.split(".", 1)[0]

from enoch.runtime_dependencies import activate_runtime_dependencies


activate_runtime_dependencies()

from our_ark_provider_kit import (  # noqa: E402
    AgentIdentity,
    AgentRuntime,
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    Attachment,
    AttachmentProvider,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ConversationId,
    Cursor,
    ForgeProvider,
    ForgeProviderError,
    EvolutionProvenance,
    LocalPublishResult,
    MessageId,
    PullRequestCloseResult,
    PullRequestMergeCandidate,
    PullRequestMergeResult,
    PullRequestMergeStatus,
    PullRequestResult,
    PullRequestTarget,
    ProgressCallback,
    ProviderHealth,
    RemotePublishResult,
    ServiceProvider,
    ServiceProviderError,
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
    "AttachmentProvider",
    "ChatEvent",
    "ChatProvider",
    "ChatProviderError",
    "ConversationId",
    "Cursor",
    "ForgeProvider",
    "ForgeProviderError",
    "EvolutionProvenance",
    "LocalPublishResult",
    "MessageId",
    "PullRequestCloseResult",
    "PullRequestMergeCandidate",
    "PullRequestMergeResult",
    "PullRequestMergeStatus",
    "PullRequestResult",
    "PullRequestTarget",
    "ProgressCallback",
    "ProviderHealth",
    "RemotePublishResult",
    "ServiceProvider",
    "ServiceProviderError",
    "VersionControlProvider",
    "VersionControlProviderError",
    "normalize_conversation_id",
    "normalize_message_id",
]
