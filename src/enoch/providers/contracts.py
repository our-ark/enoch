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

try:
    from our_ark_provider_kit import (
        RUNTIME_CONTRACT_VERSION,
        RuntimeEvent,
        RuntimeOutputReference,
        RuntimeResult,
        RuntimeResultLike,
        RuntimeSideEffect,
        RuntimeUsage,
        normalize_runtime_result,
    )
except ImportError:  # provider-kit 0.1 compatibility during contract rollout
    from enoch.providers._runtime_result_compat import (
        RUNTIME_CONTRACT_VERSION,
        RuntimeEvent,
        RuntimeOutputReference,
        RuntimeResult,
        RuntimeResultLike,
        RuntimeSideEffect,
        RuntimeUsage,
        normalize_runtime_result,
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
    "RUNTIME_CONTRACT_VERSION",
    "RemotePublishResult",
    "RuntimeEvent",
    "RuntimeOutputReference",
    "RuntimeResult",
    "RuntimeResultLike",
    "RuntimeSideEffect",
    "RuntimeUsage",
    "ServiceProvider",
    "ServiceProviderError",
    "VersionControlProvider",
    "VersionControlProviderError",
    "normalize_conversation_id",
    "normalize_message_id",
    "normalize_runtime_result",
]
