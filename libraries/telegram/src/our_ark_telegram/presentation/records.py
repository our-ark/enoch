from __future__ import annotations

from our_ark_telegram.presentation.model import TelegramBlock
from our_ark_telegram.presentation.syntax import LABEL_RE, LIST_RE


MIN_RECORD_FIELDS = 2


def structured_text_blocks(text: str) -> list[TelegramBlock]:
    """Group multi-field list entries without changing their source text."""

    if not text:
        return []
    lines = text.splitlines(keepends=True)
    blocks: list[TelegramBlock] = []
    plain_start = 0
    index = 0
    while index < len(lines):
        end = _record_end(lines, index)
        if end is None:
            index += 1
            continue
        if plain_start < index:
            blocks.append(
                TelegramBlock(
                    "text",
                    "".join(lines[plain_start:index]),
                )
            )
        blocks.append(
            TelegramBlock(
                "record",
                "".join(lines[index:end]),
            )
        )
        index = end
        plain_start = end
    if plain_start < len(lines):
        blocks.append(
            TelegramBlock(
                "text",
                "".join(lines[plain_start:]),
            )
        )
    return blocks or [TelegramBlock("text", text)]


def _record_end(lines: list[str], start: int) -> int | None:
    header = _line_text(lines[start])
    listed = LIST_RE.fullmatch(header)
    if listed is None or listed.group("indent"):
        return None

    field_count = 0
    index = start + 1
    while index < len(lines):
        line = _line_text(lines[index])
        if not line.strip():
            index += 1
            continue
        next_list = LIST_RE.fullmatch(line)
        if next_list is not None and not next_list.group("indent"):
            break
        if not line[:1].isspace():
            break
        if LABEL_RE.fullmatch(line.strip()) is not None:
            field_count += 1
        index += 1
    return index if field_count >= MIN_RECORD_FIELDS else None


def _line_text(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line
