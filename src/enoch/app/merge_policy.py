"""Per-task merge overrides for the "land before next" PR policy.

The human sets the global policy with ``/pr auto-merge on|off`` (default off).
One request can override it with a documented argument prefix that the ``do``
and ``task`` handlers parse and strip before the request is queued:

``--merge=auto``   merge this task's review when it is ready, even when off
``--merge=never``  never auto-merge this task's review, even when on

Every leading token that starts with ``--`` is an option and must have the
form ``--<key>=<value>``. Options are validated strictly against an
allowlist: a malformed token, an unknown key, an unknown value, or a repeated
key rejects the whole request instead of being guessed at. Text after the
options is the request, unchanged.

The override is recorded on the task (``TaskJob.merge_policy``). It comes only
from the human's own words: the conversation agent adds the prefix only when
the human explicitly says so, and a typed ``/do`` or ``/task`` carries it
verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


MERGE_AUTO = "auto"
MERGE_NEVER = "never"
MERGE_POLICIES = frozenset({MERGE_AUTO, MERGE_NEVER})
# Allowed request options and their allowed values.
REQUEST_OPTIONS: dict[str, frozenset[str]] = {"merge": MERGE_POLICIES}
_OPTION = re.compile(r"--(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>\S*)")
_LABELS = {MERGE_AUTO: "merge when ready", MERGE_NEVER: "no merge"}


class RequestOptionError(ValueError):
    """A request option prefix is not in the allowlist or has a bad value."""


@dataclass(frozen=True)
class RequestOptions:
    request: str
    merge: str = ""


def split_request_options(argument: str) -> RequestOptions:
    """Strip and validate leading ``--key=value`` options from a do/task request."""

    rest = argument.strip()
    values: dict[str, str] = {}
    while rest.startswith("--"):
        token, remainder = _first_token(rest)
        match = _OPTION.fullmatch(token)
        if match is None:
            raise RequestOptionError(
                f"Could not read request option {token!r}. Use --merge=auto or --merge=never."
            )
        key = match.group("key").lower()
        value = match.group("value").lower()
        allowed = REQUEST_OPTIONS.get(key)
        if allowed is None:
            raise RequestOptionError(
                f"Unknown request option --{key}. Allowed: "
                + ", ".join(f"--{name}={'|'.join(sorted(choices))}" for name, choices in REQUEST_OPTIONS.items())
                + "."
            )
        if value not in allowed:
            raise RequestOptionError(
                f"--{key} must be one of: {', '.join(sorted(allowed))}."
            )
        if key in values:
            raise RequestOptionError(f"--{key} was given more than once.")
        values[key] = value
        rest = remainder.strip()
    return RequestOptions(request=rest, merge=values.get("merge", ""))


def _first_token(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def normalize_merge_policy(value: object) -> str:
    """The stored per-task override: ``auto``, ``never``, or empty (follow the setting)."""

    text = str(value or "").strip().lower()
    return text if text in MERGE_POLICIES else ""


def merge_policy_label(policy: str) -> str:
    """``merge when ready``, ``no merge``, or empty for the default."""

    return _LABELS.get(normalize_merge_policy(policy), "")


def wants_auto_merge(policy: str, *, enabled: bool) -> bool:
    """Whether a finished task's review should be registered for merging."""

    policy = normalize_merge_policy(policy)
    if policy == MERGE_NEVER:
        return False
    return policy == MERGE_AUTO or enabled
