from __future__ import annotations

from dataclasses import dataclass
import json
import re


EDIT_REQUEST_START = "[ENOCH_EDIT_REQUEST]"
EDIT_REQUEST_END = "[/ENOCH_EDIT_REQUEST]"
MEMORY_REQUEST_START = "[ENOCH_MEMORY_REQUEST]"
MEMORY_REQUEST_END = "[/ENOCH_MEMORY_REQUEST]"
TASK_REGRESSION_START = "[ENOCH_TASK_REGRESSION]"
TASK_REGRESSION_END = "[/ENOCH_TASK_REGRESSION]"


@dataclass(frozen=True)
class EditRequest:
    visible_reply: str
    request: str


@dataclass(frozen=True)
class MemoryRequests:
    visible_reply: str
    requests: tuple[str, ...]


@dataclass(frozen=True)
class TaskRegressionSignal:
    task_id: int
    reason: str
    resolution: str = ""
    fix_task_id: int | None = None


@dataclass(frozen=True)
class TaskRegressionSignals:
    visible_reply: str
    signals: tuple[TaskRegressionSignal, ...]


def read_only_turn_prompt(message: str) -> str:
    return _with_blocks(
        message,
        [
            _read_only_wrapper_block(),
            _state_freshness_block(),
            _memory_request_block(),
            _task_regression_block(),
        ],
    )


def work_request_prompt(request: str) -> str:
    return _with_blocks(
        f"Proceed with this work request:\n{request.strip()}",
        [
            _work_request_wrapper_block(),
            _state_freshness_block(),
            _memory_request_block(),
            _task_regression_block(),
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


def extract_task_regression_signals(reply: str) -> TaskRegressionSignals:
    signals = []
    for payload in _task_regression_pattern().findall(reply):
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        task_id = _positive_int(raw.get("task_id"))
        reason = " ".join(str(raw.get("reason") or "").split())
        resolution = str(raw.get("resolution") or "").strip().lower()
        fix_task_id = _positive_int(raw.get("fix_task_id"))
        if (
            task_id is None
            or not reason
            or resolution not in {"", "reverted", "forward-fixed"}
        ):
            continue
        signals.append(
            TaskRegressionSignal(
                task_id=task_id,
                reason=reason,
                resolution=resolution,
                fix_task_id=fix_task_id,
            )
        )
    visible = _task_regression_pattern().sub("", reply).strip()
    return TaskRegressionSignals(visible_reply=visible, signals=tuple(signals))


def repository_handoff_note(branch: str, pr_url: str, resident_branch: str = "main") -> str:
    return "\n".join(
        [
            "Repository state update:",
            f"Your last edits were committed to `{branch}` and opened as `{pr_url}`.",
            f"Local checkout is back on resident branch `{resident_branch}`.",
            "",
            "Do not assume the PR was merged.",
            f"Do not assume `{resident_branch}` contains those changes.",
            "Inspect current local state before repository-dependent work.",
            "Treat the current repository state and `origin/main` as source of truth "
            "unless asked to continue/update that PR.",
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
            "The current checkout is already an isolated task worktree on its task branch.",
            "Stay on the current branch; do not switch to or check out main, which may belong to another worktree.",
            "When the requested work requires publishing an existing branch or creating a pull request, do it directly.",
            "When implementation is complete and validation passes, publish the pull request as ready for review, not draft.",
            "Use a draft pull request only when work is intentionally incomplete or the human explicitly requests a draft.",
            "When the task supplies an Evolution provenance section, preserve it verbatim in the pull request body.",
            "Never merge a pull request from a work request. Only an explicit human /pr merge <PR number or GitHub PR URL> command from Enoch's locked Telegram chat authorizes that exact merge.",
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


def _task_regression_block() -> str:
    return "\n".join(
        [
            "Task regression journal:",
            "Enoch owns regression bookkeeping; never ask the human to maintain task statuses or use a regression command.",
            "When there is clear evidence that a previously completed Enoch task introduced a regression, inspect the local task history to identify the original task id.",
            "Include exactly one internal JSON signal for that original task:",
            (
                f'{TASK_REGRESSION_START}\n'
                '{"task_id": <original-task-id>, "reason": "<concise evidence>", '
                '"resolution": "<empty, reverted, or forward-fixed>", '
                '"fix_task_id": <completed-fix-task-id or null>}\n'
                f"{TASK_REGRESSION_END}"
            ),
            "Use an empty resolution when the regression is only identified and remains unresolved.",
            "Use reverted only after the change was actually rolled back.",
            "Use forward-fixed only after the corrective work is complete.",
            "During a work request, leave fix_task_id null so the wrapper links the current task after it completes.",
            "Outside a work request, provide the already completed fix task id; otherwise leave the regression unresolved.",
            "Emit no signal when the task id or regression evidence is uncertain.",
            "Do not edit task_events.jsonl, task_queue.json, or evolve_events.jsonl directly.",
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


def _task_regression_pattern() -> re.Pattern[str]:
    return re.compile(
        rf"{re.escape(TASK_REGRESSION_START)}(.*?){re.escape(TASK_REGRESSION_END)}",
        re.DOTALL,
    )


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
