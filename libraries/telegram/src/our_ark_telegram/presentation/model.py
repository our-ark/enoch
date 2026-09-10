from __future__ import annotations

from dataclasses import dataclass


TELEGRAM_BREAK = "<!-- telegram:break -->"


@dataclass(frozen=True)
class TelegramMessageChunk:
    """One safe Telegram HTML payload and its exact plain-text fallback."""

    html: str
    plain: str


@dataclass(frozen=True)
class TelegramBlock:
    """One logical Telegram rendering and chunking unit."""

    kind: str
    text: str
