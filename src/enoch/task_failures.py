from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskFailure:
    code: str
    failure_class: str
    retryable: bool


_PERMANENT_FAILURES = (
    (
        "runtime_not_found",
        (
            "cannot find the codex cli",
            "codex binary was not found",
            "configured executable",
            "does not exist or is not executable",
        ),
    ),
    (
        "dirty_worktree",
        (
            "worktree is not clean",
            "worktree path is not empty",
        ),
    ),
    (
        "worktree_precondition",
        (
            "worktree is on",
            "expected branch",
            "does not own its execution lease",
            "lost its execution lease",
            "local branch",
            "does not exist",
        ),
    ),
    (
        "validation_failed",
        (
            "doctor failed",
            "tests failed",
            "test suite failed",
        ),
    ),
    (
        "merge_conflict",
        (
            "merge conflict",
            "could not apply",
            "conflict prevents",
        ),
    ),
    (
        "permission_denied",
        (
            "permission denied",
            "operation not permitted",
            "authentication failed",
            "authorization failed",
            "not authorized",
        ),
    ),
    (
        "invalid_request",
        (
            "invalid request",
            "invalid argument",
            "unsupported command",
        ),
    ),
)

_TRANSIENT_FAILURES = (
    (
        "rate_limited",
        (
            "rate limit",
            "too many requests",
            "status 429",
            "http 429",
        ),
    ),
    (
        "service_unavailable",
        (
            "temporarily unavailable",
            "temporary failure",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "status 500",
            "status 502",
            "status 503",
            "status 504",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        ),
    ),
    (
        "network_error",
        (
            "connection reset",
            "connection aborted",
            "connection refused",
            "remote end closed",
            "network is unreachable",
            "network unreachable",
            "could not resolve host",
            "failed to resolve",
            "name resolution",
            "dns failure",
            "broken pipe",
        ),
    ),
    (
        "network_timeout",
        (
            "connection timed out",
            "request timed out",
            "read timed out",
            "connect timeout",
        ),
    ),
)


def classify_task_failure(message: str) -> TaskFailure:
    normalized = " ".join(message.lower().split())
    if "task exceeded the configured" in normalized and "timeout" in normalized:
        return TaskFailure(
            code="task_timeout",
            failure_class="permanent",
            retryable=False,
        )
    for code, patterns in _PERMANENT_FAILURES:
        if any(pattern in normalized for pattern in patterns):
            return TaskFailure(code=code, failure_class="permanent", retryable=False)
    for code, patterns in _TRANSIENT_FAILURES:
        if any(pattern in normalized for pattern in patterns):
            return TaskFailure(code=code, failure_class="transient", retryable=True)
    return TaskFailure(code="unknown_failure", failure_class="permanent", retryable=False)


def interrupted_task_failure() -> TaskFailure:
    return TaskFailure(
        code="worker_interrupted",
        failure_class="transient",
        retryable=True,
    )


def automatic_retry_delay_seconds(attempt: int) -> int:
    return min(300, 30 * (2 ** max(0, attempt - 1)))
