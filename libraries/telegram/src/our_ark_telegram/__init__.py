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
    "MAX_TELEGRAM_MESSAGE",
    "READ_ACK_EMOJI",
    "TELEGRAM_API",
    "TelegramClient",
    "TelegramConfig",
    "TelegramError",
    "create_provider",
    "load_config",
    "setup_provider",
    "chunks",
    "telegram_event",
]
