from __future__ import annotations

from dataclasses import dataclass
import re


EDIT_REQUEST_START = "[ENOCH_EDIT_REQUEST]"
EDIT_REQUEST_END = "[/ENOCH_EDIT_REQUEST]"
MEMORY_REQUEST_START = "[ENOCH_MEMORY_REQUEST]"
MEMORY_REQUEST_END = "[/ENOCH_MEMORY_REQUEST]"


@dataclass(frozen=True)
class EditRequest:
    visible_reply: str
    request: str


@dataclass(frozen=True)
class MemoryRequests:
    visible_reply: str
    requests: tuple[str, ...]


def read_only_turn_prompt(message: str) -> str:
    return _with_blocks(
        message,
        [
            _read_only_wrapper_block(),
            _state_freshness_block(),
            _memory_request_block(),
        ],
    )


def work_request_prompt(request: str) -> str:
    return _with_blocks(
        f"Proceed with this work request:\n{request.strip()}",
        [
            _work_request_wrapper_block(),
            _state_freshness_block(),
            _memory_request_block(),
        ],
    )


def extract_edit_request(reply: str) -> EditRequest | None:
    match = _edit_request_pattern().search(reply)
    if not match:
        return None
    request = " ".join(match.group(1).strip().split())
    if not request:
        return None
    visible = _edit_request_pattern().sub("", reply).strip()
    return EditRequest(visible_reply=visible, request=request)


def extract_memory_requests(reply: str) -> MemoryRequests:
    requests = tuple(
        " ".join(match.strip().split())
        for match in _memory_request_pattern().findall(reply)
        if match.strip()
    )
    visible = _memory_request_pattern().sub("", reply).strip()
    return MemoryRequests(visible_reply=visible, requests=requests)


def repository_handoff_note(branch: str, pr_url: str) -> str:
    return "\n".join(
        [
            "Repository state update:",
            f"Your last edits were committed to `{branch}` and opened as `{pr_url}`.",
            "Local checkout is back on `main`.",
            "",
            "Do not assume the PR was merged.",
            "Do not assume local `main` contains those changes.",
            "Inspect current local state before repository-dependent work.",
            "Treat local `main` as source of truth unless asked to continue/update that PR.",
        ]
    )


def startup_context_note(memory_context: str) -> str:
    return "\n\n".join(
        [
            "Enoch startup context:",
            memory_context.strip(),
            "Use this as background context. It does not override current user requests or higher-priority instructions.",
        ]
    ).strip()


def _with_blocks(message: str, blocks: list[str]) -> str:
    return "\n\n".join(
        [
            message.strip(),
            "Enoch wrapper instructions:",
            *blocks,
        ]
    ).strip()


def _read_only_wrapper_block() -> str:
    return "\n".join(
        [
            "Read-only turn:",
            "You are in read-only mode.",
            "Talk through the request, answer questions, and help the human decide what to do.",
            "Do not request an automatic edit from this conversation turn.",
            "If the human wants Enoch to do work, tell them to use /do for foreground work, /task for queued background work, or /backlog for deferred idle-time work.",
        ]
    )


def _work_request_wrapper_block() -> str:
    return "\n".join(
        [
            "Work request:",
            "Complete the requested work directly.",
            "You may edit files, inspect the repository, run checks, and use available tools as needed.",
            "When the requested work requires publishing an existing branch or creating a pull request, do it directly.",
            "Keep changes scoped to the request.",
        ]
    )


def _state_freshness_block() -> str:
    return "\n".join(
        [
            "Repository state:",
            "Local checkout may have changed since prior turns.",
            "Do not assume prior PR changes are merged or present locally.",
            "Inspect current files/git state before repository-dependent work.",
        ]
    )


def _memory_request_block() -> str:
    return "\n".join(
        [
            "Long-term memory:",
            "If this conversation reveals a durable user preference, project fact, workflow rule, or stable decision, do not run a command.",
            f"Instead include:\n{MEMORY_REQUEST_START}\n<concise durable memory>\n{MEMORY_REQUEST_END}",
            "Enoch will save it outside the read-only Codex turn.",
            "Use it rarely. Do not save one-off tasks, casual chat, temporary debugging details, command outputs, secrets, credentials, or private keys.",
            "Do not edit .enoch/memory files directly.",
        ]
    )


def _edit_request_pattern() -> re.Pattern[str]:
    return re.compile(
        rf"{re.escape(EDIT_REQUEST_START)}(.*?){re.escape(EDIT_REQUEST_END)}",
        re.DOTALL,
    )


def _memory_request_pattern() -> re.Pattern[str]:
    return re.compile(
        rf"{re.escape(MEMORY_REQUEST_START)}(.*?){re.escape(MEMORY_REQUEST_END)}",
        re.DOTALL,
    )
