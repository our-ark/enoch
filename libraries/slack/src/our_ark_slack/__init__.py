from our_ark_slack.core import (
    MAX_SLACK_MARKDOWN,
    SLACK_COMMAND,
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
    "MAX_SLACK_MARKDOWN",
    "SLACK_COMMAND",
    "SlackClient",
    "SlackConfig",
    "SlackError",
    "create_provider",
    "load_config",
    "setup_provider",
    "slack_event",
    "slack_message_chunks",
]
