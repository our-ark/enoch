from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
from urllib.parse import urlsplit


_FENCE_RE = re.compile(
    r"^[ \t]*```(?P<language>[A-Za-z0-9_+.-]*)[ \t]*(?:\r?\n)?$"
)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_QUOTE_RE = re.compile(r"^(?P<indent>[ \t]*)>[ \t]?(?P<text>.*)$")
_LIST_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])(?P<space>[ \t]+)(?P<text>.*)$"
)
_HELP_COMMAND_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<command>/[A-Za-z][\w-]*(?:[ \t]+.*?)*?)"
    r"(?P<separator>[ \t]+-[ \t]+)(?P<description>.+)$"
)
_LABEL_RE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9 _()/.-]{0,47}:)"
    r"(?P<space>[ \t]*)(?P<value>.*)$"
)
_PATH_RE = re.compile(
    r"""
    (?<![\w:/])
    (
        (?!\d+/\d+\b)
        (?:~?/|\./|\.\./)
        [^\s<>()\[\]{}"']*
        [A-Za-z0-9_./~-]
        (?::\d+)?
      |
        (?!\d+/\d+\b)
        (?:[A-Za-z0-9_.-]+/)+
        [A-Za-z0-9_.-]+
        (?::\d+)?
      |
        \b[A-Za-z0-9_.-]+\.
        (?:py|md|toml|ya?ml|jsonl?|txt|sh|bash|zsh|js|jsx|ts|tsx|rs|go|java|rb|lock)
        (?::\d+)?
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"^(?:"
    r"Task #\d+\b|"
    r"Doctor\b|"
    r"Enoch\b|"
    r"Open pull requests\b|"
    r"Pull request #\d+\b|"
    r"Work status\b|"
    r"Task worktrees\b|"
    r"Tasks:|Cron:|Backlog:"
    r")",
    re.IGNORECASE,
)
_CODE_VALUE_LABELS = {
    "authoritative",
    "branch",
    "changed",
    "check command",
    "command",
    "commit",
    "executable",
    "file",
    "files",
    "local",
    "merge commit",
    "path",
    "running",
    "worktree",
}
_FORMATTING_ERROR_MARKERS = (
    "can't parse entities",
    "cant parse entities",
    "can't find end tag",
    "unsupported start tag",
    "unsupported end tag",
    "wrong entity",
)


@dataclass(frozen=True)
class TelegramMessageChunk:
    """One safe Telegram HTML payload and its plain-text fallback."""

    html: str
    plain: str


@dataclass(frozen=True)
class _Block:
    kind: str
    text: str


def render_telegram_html(text: str) -> str:
    """Render a conservative Markdown-like subset as Telegram-safe HTML."""

    return "".join(_render_block(block) for block in _parse_blocks(text))


def telegram_message_chunks(
    text: str,
    size: int,
) -> list[TelegramMessageChunk]:
    """Split on logical boundaries and render independently valid HTML chunks."""

    if size < 1:
        raise ValueError("Chunk size must be at least 1.")
    blocks = [
        piece
        for block in _parse_blocks(text)
        for piece in _split_block(block, size)
    ]
    if not blocks:
        return [TelegramMessageChunk(html="", plain="")]

    groups: list[list[_Block]] = []
    current: list[_Block] = []
    current_size = 0
    for block in blocks:
        block_size = len(block.text)
        if current and current_size + block_size > size:
            groups.append(current)
            current = []
            current_size = 0
        current.append(block)
        current_size += block_size
    if current:
        groups.append(current)

    return [
        TelegramMessageChunk(
            html="".join(_render_block(block) for block in group),
            plain="".join(block.text for block in group),
        )
        for group in groups
    ]


def is_formatting_error(error: BaseException) -> bool:
    """Return whether Telegram rejected entity markup rather than delivery."""

    message = str(error).lower()
    return any(marker in message for marker in _FORMATTING_ERROR_MARKERS)


def _parse_blocks(text: str) -> list[_Block]:
    lines = text.splitlines(keepends=True)
    if not lines and text:
        lines = [text]
    blocks: list[_Block] = []
    regular: list[str] = []
    index = 0

    def flush_regular() -> None:
        if regular:
            blocks.append(_Block("text", "".join(regular)))
            regular.clear()

    while index < len(lines):
        opening = _FENCE_RE.fullmatch(lines[index])
        if opening is None:
            regular.append(lines[index])
            index += 1
            continue

        flush_regular()
        index += 1
        code_lines: list[str] = []
        closing_line = ""
        while index < len(lines):
            if _FENCE_RE.fullmatch(lines[index]):
                closing_line = lines[index]
                index += 1
                break
            code_lines.append(lines[index])
            index += 1

        code = "".join(code_lines)
        if closing_line:
            code = _remove_one_trailing_newline(code)
        blocks.append(_Block("code", code))
        if closing_line.endswith(("\n", "\r")) and index < len(lines):
            blocks.append(_Block("text", "\n"))

    flush_regular()
    return blocks


def _split_block(block: _Block, size: int) -> list[_Block]:
    if len(block.text) <= size:
        return [block]
    parts = _split_text(block.text, size, prefer_paragraphs=block.kind == "text")
    return [_Block(block.kind, part) for part in parts]


def _split_text(text: str, size: int, *, prefer_paragraphs: bool) -> list[str]:
    remaining = text
    parts: list[str] = []
    while len(remaining) > size:
        window = remaining[: size + 1]
        cuts: list[int] = []
        if prefer_paragraphs:
            paragraph = window.rfind("\n\n", 0, size + 1)
            if paragraph >= 0:
                cuts.append(paragraph + 2)
        newline = window.rfind("\n", 0, size + 1)
        if newline >= 0:
            cuts.append(newline + 1)
        if prefer_paragraphs:
            whitespace = max(
                window.rfind(" ", 0, size + 1),
                window.rfind("\t", 0, size + 1),
            )
            if whitespace >= 0:
                cuts.append(whitespace + 1)
        viable = [cut for cut in cuts if cut >= max(1, size // 3)]
        cut = max(viable, default=size)
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining or not parts:
        parts.append(remaining)
    return parts


def _render_block(block: _Block) -> str:
    if block.kind == "code":
        return f"<pre><code>{escape(block.text, quote=False)}</code></pre>"
    return _render_text(block.text)


def _render_text(text: str) -> str:
    rendered: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        line, ending = _line_and_ending(raw_line)
        rendered.append(_render_line(line))
        rendered.append(ending)
    if text and not text.endswith(("\n", "\r")) and not rendered:
        rendered.append(_render_line(text))
    return "".join(rendered)


def _render_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return escape(line, quote=False)

    heading = _HEADING_RE.fullmatch(line)
    if heading is not None:
        return f"<b>{escape(heading.group('text'), quote=False)}</b>"

    quote = _QUOTE_RE.fullmatch(line)
    if quote is not None:
        return (
            f"{escape(quote.group('indent'), quote=False)}"
            f"<blockquote>{escape(quote.group('text'), quote=False)}</blockquote>"
        )

    help_command = _HELP_COMMAND_RE.fullmatch(line)
    if help_command is not None:
        return "".join(
            [
                escape(help_command.group("indent"), quote=False),
                f"<code>{escape(help_command.group('command'), quote=False)}</code>",
                escape(help_command.group("separator"), quote=False),
                _render_inline(help_command.group("description")),
            ]
        )

    listed = _LIST_RE.fullmatch(line)
    if listed is not None:
        content = _render_labeled_text(
            listed.group("text"),
            allow_lowercase=True,
        ) or _render_inline(listed.group("text"))
        return "".join(
            [
                escape(listed.group("indent"), quote=False),
                escape(listed.group("marker"), quote=False),
                escape(listed.group("space"), quote=False),
                content,
            ]
        )

    if len(stripped) <= 80 and _TITLE_RE.match(stripped):
        prefix = line[: len(line) - len(line.lstrip())]
        return (
            f"{escape(prefix, quote=False)}"
            f"<b>{escape(line.lstrip(), quote=False)}</b>"
        )

    labeled = _render_labeled_text(line, allow_lowercase=False)
    if labeled is not None:
        return labeled

    if _looks_like_shell_command(line):
        return f"<code>{escape(line, quote=False)}</code>"
    return _render_inline(line)


def _render_labeled_text(text: str, *, allow_lowercase: bool) -> str | None:
    match = _LABEL_RE.fullmatch(text)
    if match is None:
        return None
    label = match.group("label")
    name = label[:-1].strip()
    if name.lower() in {"http", "https"}:
        return None
    if not allow_lowercase and not name[:1].isupper():
        return None

    value = match.group("value")
    rendered_value = _render_label_value(name, value)
    return "".join(
        [
            f"<b>{escape(label, quote=False)}</b>",
            escape(match.group("space"), quote=False),
            rendered_value,
        ]
    )


def _render_label_value(label: str, value: str) -> str:
    normalized = label.strip().lower()
    if value and normalized in _CODE_VALUE_LABELS:
        return f"<code>{escape(value, quote=False)}</code>"
    return _render_inline(value)


def _render_inline(text: str) -> str:
    rendered: list[str] = []
    plain_start = 0
    index = 0

    def flush_plain(end: int) -> None:
        nonlocal plain_start
        if end > plain_start:
            rendered.append(_render_plain(text[plain_start:end]))
        plain_start = end

    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            flush_plain(index)
            rendered.append(escape(text[index + 1], quote=False))
            index += 2
            plain_start = index
            continue

        if text[index] == "`":
            end = _find_unescaped(text, "`", index + 1)
            if end > index + 1:
                flush_plain(index)
                rendered.append(
                    f"<code>{escape(text[index + 1:end], quote=False)}</code>"
                )
                index = end + 1
                plain_start = index
                continue

        if text[index] == "[":
            link = _parse_link(text, index)
            if link is not None:
                end, label, target = link
                flush_plain(index)
                rendered.append(_render_link(label, target))
                index = end
                plain_start = index
                continue

        marker = next(
            (
                candidate
                for candidate in ("**", "~~")
                if text.startswith(candidate, index)
            ),
            "",
        )
        if marker:
            end = _find_unescaped(text, marker, index + len(marker))
            if end > index + len(marker):
                flush_plain(index)
                tag = "b" if marker == "**" else "s"
                content = escape(
                    text[index + len(marker):end],
                    quote=False,
                )
                rendered.append(f"<{tag}>{content}</{tag}>")
                index = end + len(marker)
                plain_start = index
                continue

        if text[index] == "*" and _valid_emphasis_open(text, index):
            marker = text[index]
            end = _find_emphasis_end(text, marker, index + 1)
            if end > index + 1:
                flush_plain(index)
                rendered.append(
                    f"<i>{escape(text[index + 1:end], quote=False)}</i>"
                )
                index = end + 1
                plain_start = index
                continue

        index += 1

    flush_plain(len(text))
    return "".join(rendered)


def _render_plain(text: str) -> str:
    rendered: list[str] = []
    position = 0
    for match in _PATH_RE.finditer(text):
        rendered.append(escape(text[position:match.start()], quote=False))
        rendered.append(f"<code>{escape(match.group(0), quote=False)}</code>")
        position = match.end()
    rendered.append(escape(text[position:], quote=False))
    return "".join(rendered)


def _render_link(label: str, target: str) -> str:
    cleaned_target = target.strip()
    if cleaned_target.startswith("<") and cleaned_target.endswith(">"):
        cleaned_target = cleaned_target[1:-1].strip()
    scheme = urlsplit(cleaned_target).scheme.lower()
    if scheme in {"http", "https"}:
        return (
            f'<a href="{escape(cleaned_target, quote=True)}">'
            f"{escape(label, quote=False)}</a>"
        )
    if _looks_like_local_reference(cleaned_target):
        rendered_label = f"<code>{escape(label, quote=False)}</code>"
        if cleaned_target == label:
            return rendered_label
        return (
            f"{rendered_label} "
            f"(<code>{escape(cleaned_target, quote=False)}</code>)"
        )
    return (
        f"{escape(label, quote=False)} "
        f"(<code>{escape(cleaned_target, quote=False)}</code>)"
    )


def _parse_link(text: str, start: int) -> tuple[int, str, str] | None:
    label_end = _find_unescaped(text, "]", start + 1)
    if label_end < 0 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None
    depth = 1
    index = label_end + 2
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return (
                    index + 1,
                    text[start + 1:label_end],
                    text[label_end + 2:index],
                )
        index += 1
    return None


def _find_unescaped(text: str, marker: str, start: int) -> int:
    index = start
    while True:
        index = text.find(marker, index)
        if index < 0:
            return -1
        preceding = 0
        cursor = index - 1
        while cursor >= 0 and text[cursor] == "\\":
            preceding += 1
            cursor -= 1
        if preceding % 2 == 0:
            return index
        index += len(marker)


def _valid_emphasis_open(text: str, index: int) -> bool:
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    return (
        bool(following)
        and not following.isspace()
        and (not previous or previous.isspace() or previous in "([{>:;,-")
    )


def _find_emphasis_end(text: str, marker: str, start: int) -> int:
    index = start
    while True:
        index = _find_unescaped(text, marker, index)
        if index < 0:
            return -1
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if previous and not previous.isspace() and (
            not following or following.isspace() or following in ".,!?;:)]}>-"
        ):
            return index
        index += 1


def _looks_like_local_reference(target: str) -> bool:
    if target.startswith(("/", "./", "../", "~/")):
        return True
    return not urlsplit(target).scheme and "/" in target


def _looks_like_shell_command(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    first = stripped.split(maxsplit=1)[0]
    if first in {
        "$",
        "python",
        "python3",
        "pip",
        "pip3",
        "git",
        "gh",
        "pytest",
        "uv",
    }:
        return True
    return first.startswith(("bin/", "./bin/", "/opt/", "/usr/"))


def _line_and_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _remove_one_trailing_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith(("\n", "\r")):
        return text[:-1]
    return text
