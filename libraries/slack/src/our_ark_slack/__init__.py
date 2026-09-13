from our_ark_slack.core import (
    DEFAULT_COMMAND_PREFIX,
    MAX_SLACK_MARKDOWN,
    SECONDARY_COMMAND_PREFIX,
    SlackClient,
    SlackConfig,
    SlackError,
    slack_event,
    slack_message_chunks,
)
from our_ark_slack.integration import load_config, setup_provider


def create_provider(root=None):
    from our_ark_slack.integration import create_provider as factory

    return factory(root)


OUR_ARK_PROVIDERS = (
    {
        "kind": "chat",
        "name": "slack",
        "factory": create_provider,
        "setup": setup_provider,
    },
)


__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "MAX_SLACK_MARKDOWN",
    "SECONDARY_COMMAND_PREFIX",
    "SlackClient",
    "SlackConfig",
    "SlackError",
    "create_provider",
    "load_config",
    "setup_provider",
    "slack_event",
    "slack_message_chunks",
]
