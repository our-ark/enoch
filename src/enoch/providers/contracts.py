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
        CAPABILITY_CONTRACT_VERSION,
        AuthorizationDecision,
        AuthorizationPolicy,
        AuthorizationRequest,
        CapabilityProvider,
        ProviderCapabilities,
        TaskRequirements,
    )
except ImportError:  # provider-kit compatibility during capability rollout
    from enoch.providers._capability_compat import (
        CAPABILITY_CONTRACT_VERSION,
        AuthorizationDecision,
        AuthorizationPolicy,
        AuthorizationRequest,
        CapabilityProvider,
        ProviderCapabilities,
        TaskRequirements,
    )

try:
    from our_ark_provider_kit import (
        AgentRuntimeTimedOut,
        RUNTIME_CONTRACT_VERSION,
        RUNTIME_EXECUTION_CONTRACT_VERSION,
        RuntimeEvent,
        RuntimeExecutionControl,
        RuntimeOutputReference,
        RuntimeProgress,
        RuntimeProgressCallback,
        RuntimeResult,
        RuntimeResultLike,
        RuntimeSideEffect,
        RuntimeUsage,
        normalize_runtime_result,
    )
except ImportError:  # provider-kit 0.1 compatibility during contract rollout
    from enoch.providers._runtime_result_compat import (
        AgentRuntimeTimedOut,
        RUNTIME_CONTRACT_VERSION,
        RUNTIME_EXECUTION_CONTRACT_VERSION,
        RuntimeEvent,
        RuntimeExecutionControl,
        RuntimeOutputReference,
        RuntimeProgress,
        RuntimeProgressCallback,
        RuntimeResult,
        RuntimeResultLike,
        RuntimeSideEffect,
        RuntimeUsage,
        normalize_runtime_result,
    )

try:
    from our_ark_provider_kit import (
        DurableNotificationProvider,
        NOTIFICATION_CONTRACT_VERSION,
        NotificationCapabilities,
        NotificationDeliveryError,
        NotificationIntent,
        NotificationReceipt,
    )
except ImportError:  # provider-kit compatibility during notification rollout
    from enoch.providers._notification_compat import (
        DurableNotificationProvider,
        NOTIFICATION_CONTRACT_VERSION,
        NotificationCapabilities,
        NotificationDeliveryError,
        NotificationIntent,
        NotificationReceipt,
    )

__all__ = [
    "CAPABILITY_CONTRACT_VERSION",
    "AgentIdentity",
    "AgentRuntime",
    "AgentRuntimeAccessUnavailable",
    "AgentRuntimeCancelled",
    "AgentRuntimeError",
    "AgentRuntimeTimedOut",
    "Attachment",
    "AttachmentProvider",
    "AuthorizationDecision",
    "AuthorizationPolicy",
    "AuthorizationRequest",
    "ChatEvent",
    "ChatProvider",
    "ChatProviderError",
    "CapabilityProvider",
    "ConversationId",
    "Cursor",
    "DurableNotificationProvider",
    "ForgeProvider",
    "ForgeProviderError",
    "EvolutionProvenance",
    "LocalPublishResult",
    "MessageId",
    "NOTIFICATION_CONTRACT_VERSION",
    "NotificationCapabilities",
    "NotificationDeliveryError",
    "NotificationIntent",
    "NotificationReceipt",
    "PullRequestCloseResult",
    "PullRequestMergeCandidate",
    "PullRequestMergeResult",
    "PullRequestMergeStatus",
    "PullRequestResult",
    "PullRequestTarget",
    "ProgressCallback",
    "ProviderCapabilities",
    "ProviderHealth",
    "RUNTIME_CONTRACT_VERSION",
    "RUNTIME_EXECUTION_CONTRACT_VERSION",
    "RemotePublishResult",
    "RuntimeEvent",
    "RuntimeExecutionControl",
    "RuntimeOutputReference",
    "RuntimeProgress",
    "RuntimeProgressCallback",
    "RuntimeResult",
    "RuntimeResultLike",
    "RuntimeSideEffect",
    "RuntimeUsage",
    "ServiceProvider",
    "ServiceProviderError",
    "TaskRequirements",
    "VersionControlProvider",
    "VersionControlProviderError",
    "normalize_conversation_id",
    "normalize_message_id",
    "normalize_runtime_result",
]
