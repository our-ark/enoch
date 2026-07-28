from our_ark_telegram.core import (
    MAX_TELEGRAM_MESSAGE,
    READ_ACK_EMOJI,
    TELEGRAM_API,
    TelegramClient,
    TelegramConfig,
    TelegramError,
    chunks,
    telegram_event,
)
from our_ark_telegram.integration import load_config, setup_provider
from our_ark_telegram.presentation import (
    HELP_CALLBACK_PREFIX,
    HELP_NAVIGATION_MODES,
    TelegramMessageChunk,
    render_telegram_html,
    telegram_help_callback_command,
    telegram_help_reply_markup,
    telegram_message_chunks,
)


def create_provider(root=None):
    from our_ark_telegram.integration import create_provider as factory

    return factory(root)


OUR_ARK_PROVIDERS = (
    {
        "kind": "chat",
        "name": "telegram",
        "factory": create_provider,
        "setup": setup_provider,
        "default": True,
    },
)


__all__ = [
    "HELP_CALLBACK_PREFIX",
    "HELP_NAVIGATION_MODES",
    "MAX_TELEGRAM_MESSAGE",
    "READ_ACK_EMOJI",
    "TELEGRAM_API",
    "TelegramClient",
    "TelegramConfig",
    "TelegramError",
    "TelegramMessageChunk",
    "create_provider",
    "load_config",
    "setup_provider",
    "chunks",
    "render_telegram_html",
    "telegram_help_callback_command",
    "telegram_help_reply_markup",
    "telegram_message_chunks",
    "telegram_event",
]
