from __future__ import annotations

import re


FENCE_RE = re.compile(
    r"^[ \t]*```(?P<language>[A-Za-z0-9_+.-]*)[ \t]*(?:\r?\n)?$"
)
HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$"
)
QUOTE_RE = re.compile(
    r"^(?P<indent>[ \t]*)>[ \t]?(?P<text>.*)$"
)
LIST_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])"
    r"(?P<space>[ \t]+)(?P<text>.*)$"
)
HELP_COMMAND_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<command>/[A-Za-z][\w-]*(?:[ \t]+.*?)*?)"
    r"(?P<separator>[ \t]+-[ \t]+)(?P<description>.+)$"
)
LABEL_RE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9 _()/.-]{0,47}:)"
    r"(?P<space>[ \t]*)(?P<value>.*)$"
)
PATH_RE = re.compile(
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
TITLE_RE = re.compile(
    r"^(?:"
    r"Task #\d+\b|"
    r"Doctor\b|"
    r"Enoch\b|"
    r"Evolve candidates\b|"
    r"Open pull requests\b|"
    r"Pull request #\d+\b|"
    r"Work status\b|"
    r"Task worktrees\b|"
    r"Tasks:|Cron:|Backlog:"
    r")",
    re.IGNORECASE,
)
RECORD_IDENTIFIER_RE = re.compile(
    r"^(?P<identifier>"
    r"(?:#[1-9]\d*)|"
    r"(?:[A-Za-z][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)+)"
    r")(?P<space>[ \t]+)(?P<rest>.+)$"
)
