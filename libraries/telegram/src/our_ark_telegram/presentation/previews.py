from __future__ import annotations

import re

from our_ark_telegram.presentation.syntax import URL_RE


MARKDOWN_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)"
)
TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


def preview_message_segments(text: str) -> list[str]:
    """Split comparison tables or product URLs into one-preview messages."""

    table_cards = _cards_from_markdown_table(text)
    if table_cards is not None:
        return table_cards
    product_items = _segments_for_product_urls(text)
    if product_items is not None:
        return product_items
    return [text]


def _cards_from_markdown_table(text: str) -> list[str] | None:
    lines = text.splitlines()
    tables = _markdown_tables(lines)
    if len(tables) != 1:
        return None
    start, end = tables[0]
    rows = [_split_table_cells(line) for line in lines[start:end]]
    if len(rows) < 3 or not _is_separator_row(rows[1]):
        return None
    headers = rows[0]
    cards: list[str] = []
    for cells in rows[2:]:
        card = _format_table_card(len(cards) + 1, headers, cells)
        if card is not None:
            cards.append(card)
    if len(cards) < 2:
        return None

    preamble = "\n".join(lines[:start]).strip()
    footer = "\n".join(lines[end:]).strip()
    if preamble:
        cards[0] = f"{preamble}\n\n{cards[0]}"
    if footer:
        cards[-1] = f"{cards[-1]}\n\n{footer}"
    return cards


def _markdown_tables(lines: list[str]) -> list[tuple[int, int]]:
    tables: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if _is_table_line(lines[index]):
            start = index
            index += 1
            while index < len(lines) and _is_table_line(lines[index]):
                index += 1
            if index - start >= 3:
                tables.append((start, index))
            continue
        index += 1
    return tables


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        TABLE_SEPARATOR_CELL_RE.fullmatch(cell or "") for cell in cells
    )


def _format_table_card(index: int, headers: list[str], cells: list[str]) -> str | None:
    url = ""
    name = ""
    leftover = ""
    for cell in cells:
        found_url, found_name, found_leftover = _primary_link(cell)
        if found_url:
            url = found_url
            name = found_name
            leftover = found_leftover
            break
    if not url:
        return None

    title = name or f"Option {index}"
    lines = [f"{index}. {title}"]
    if leftover:
        lines.append(f"   Store: {leftover}")
    for header, cell in zip(headers[1:], cells[1:]):
        cleaned = MARKDOWN_LINK_RE.sub(lambda match: match.group("label"), cell).strip()
        if cleaned:
            lines.append(f"   {header.strip() or 'Detail'}: {cleaned}")
    lines.append(url)
    return "\n".join(lines)


def _primary_link(cell: str) -> tuple[str, str, str]:
    match = MARKDOWN_LINK_RE.search(cell)
    if match is not None:
        leftover = f"{cell[: match.start()]}{cell[match.end() :]}"
        leftover = leftover.strip(" \t|-–—")
        return match.group("url"), match.group("label").strip(), leftover
    url_match = URL_RE.search(cell)
    if url_match is not None:
        leftover = f"{cell[: url_match.start()]}{cell[url_match.end() :]}".strip()
        return url_match.group(0), leftover, ""
    return "", "", ""


def _segments_for_product_urls(text: str) -> list[str] | None:
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    anchors: list[int] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        urls = [url for url in _urls_in(line) if _is_product_url(url)]
        if any(url not in seen for url in urls):
            if sum(1 for url in urls if url not in seen) > 1:
                return None
            seen.update(urls)
            anchors.append(index)
    if len(anchors) < 2:
        return None

    segments: list[str] = []
    for offset, start in enumerate(anchors):
        end = anchors[offset + 1] if offset + 1 < len(anchors) else len(lines)
        prefix = lines[:start] if offset == 0 else []
        chunk = "".join([*prefix, *lines[start:end]]).strip()
        if chunk:
            segments.append(chunk)
    return segments if len(segments) >= 2 else None


def _urls_in(text: str) -> list[str]:
    found: list[str] = []
    for match in MARKDOWN_LINK_RE.finditer(text):
        url = match.group("url")
        if url not in found:
            found.append(url)
    for url in URL_RE.findall(text):
        if url not in found:
            found.append(url)
    return found


def _is_product_url(url: str) -> bool:
    return "/products/" in url.lower()
