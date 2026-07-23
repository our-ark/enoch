from __future__ import annotations

import time

from enoch.app.models import WorkStatusMessage
from enoch.providers.contracts import PullRequestMergeCandidate, PullRequestMergeResult
from enoch.tasks.queue import TaskJob


def format_pull_request_merge_result(result: PullRequestMergeResult) -> str:
    commit = result.merge_commit or "reported by the forge"
    return "\n".join(
        [
            f"Merged PR #{result.number}.",
            f"URL: {result.url}",
            f"Method: {result.method}",
            f"Merge commit: {commit}",
            f"Forge result: {result.message}",
        ]
    )


def format_open_pull_requests(
    pull_requests: tuple[PullRequestMergeCandidate, ...],
) -> str:
    if not pull_requests:
        return "Open pull requests: none."
    lines = [f"Open pull requests ({len(pull_requests)}):"]
    for pull_request in pull_requests:
        lines.extend(
            [
                "",
                f"#{pull_request.number} [{pull_request_readiness(pull_request)}] "
                f"{pull_request.title or 'Untitled pull request'}",
                pull_request_branch_line(pull_request),
                pull_request.url,
            ]
        )
    return "\n".join(lines)


def format_pull_request(pull_request: PullRequestMergeCandidate) -> str:
    lines = [
        f"Pull request #{pull_request.number}",
        f"Title: {pull_request.title or 'Untitled pull request'}",
        f"Status: {pull_request_readiness(pull_request)}",
        f"State: {pull_request.state.lower() or 'unknown'}",
        (
            "Merge: "
            f"{pull_request.mergeable.lower() or 'unknown'} / "
            f"{pull_request.merge_state_status.lower() or 'unknown'}"
        ),
        f"Branch: {pull_request_branch_line(pull_request)}",
    ]
    if pull_request.author:
        lines.append(f"Author: {pull_request.author}")
    if pull_request.updated_at:
        lines.append(f"Updated: {pull_request.updated_at}")
    lines.append(f"URL: {pull_request.url}")
    return "\n".join(lines)


def pull_request_readiness(pull_request: PullRequestMergeCandidate) -> str:
    if pull_request.state == "MERGED" or pull_request.merged_at:
        return "merged"
    if pull_request.state == "CLOSED":
        return "closed"
    if pull_request.is_draft:
        return "draft"
    if pull_request.mergeable == "CONFLICTING":
        return "conflicts"
    if (
        pull_request.mergeable == "MERGEABLE"
        and pull_request.merge_state_status in {"CLEAN", "UNSTABLE"}
    ):
        return "ready"
    if pull_request.merge_state_status in {"BLOCKED", "DIRTY", "BEHIND"}:
        return "blocked"
    return "checking"


def pull_request_branch_line(pull_request: PullRequestMergeCandidate) -> str:
    base = pull_request.base_branch or "unknown"
    head = pull_request.head_branch or "unknown"
    return f"{base} <- {head}"


def format_elapsed(elapsed_seconds: int) -> str:
    if elapsed_seconds < 60:
        return "<1 minute"
    minutes = elapsed_seconds // 60
    if minutes < 60:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours, remaining_minutes = divmod(minutes, 60)
    hour_text = f"{hours} hour" + ("" if hours == 1 else "s")
    if remaining_minutes == 0:
        return hour_text
    minute_text = f"{remaining_minutes} minute" + ("" if remaining_minutes == 1 else "s")
    return f"{hour_text} {minute_text}"


def final_task_status_update(final_status: str) -> str:
    if final_status == "paused":
        return "Paused. Use /task resume <id|all> after agent runtime access is restored."
    if final_status == "failed":
        return "Failed. Final summary sent below."
    if final_status == "cancelled":
        return "Cancelled. Final summary sent below."
    return "Completed. Final summary sent below."


def format_task_final_message(job: TaskJob, final_status: str, result: str) -> str:
    summary = job.result or result or "No result summary was recorded."
    if final_status == "paused":
        return "\n".join(
            [
                f"Task #{job.id} paused",
                clip_activity_block(summary, limit=1200),
            ]
        )
    prs = job.pr_urls or ("none",)
    lines = [
        f"Task #{job.id} final update",
        f"Final status: {final_status}",
    ]
    if final_status == "failed" and job.failure_code:
        lines.extend(
            [
                f"Failure: {job.failure_code} ({job.failure_class or 'unknown'}, non-retryable)",
                f"Attempts: {job.attempt}/{job.max_attempts}",
            ]
        )
    lines.extend(
        [
            "PR URL:",
            *[f"- {pr}" for pr in prs],
            "Result summary:",
            clip_activity_block(summary, limit=1200),
        ]
    )
    return "\n".join(lines)


def format_work_status_message(status: WorkStatusMessage) -> str:
    elapsed = format_elapsed(max(0, int(time.monotonic() - status.started_at)))
    prs = status.prs or ["none"]
    title = f"Task #{status.task_id}" if status.task_id is not None else "Work status"
    lines = [
        title,
        f"Status: {status.status}",
        f"Time: {elapsed}",
        f"Latest update: {status.latest_update}",
        "PRs created:",
        *[f"- {pr}" for pr in prs],
        "",
        "Request:",
        clip_activity_text(status.request, limit=1200),
    ]
    if status.context:
        lines.extend(
            [
                "",
                "Conversation context snapshot:",
                clip_activity_text(status.context, limit=1200),
            ]
        )
    return "\n".join(lines)


def backlog_usage() -> str:
    return "\n".join(
        [
            "Use /backlog [p0|p1|p2] <request> to save deferred work.",
            "Use /backlog remove <id> to remove a pending backlog item.",
            "Use /backlog priority <id> p0|p1|p2 to reprioritize a pending backlog item.",
            "Use /backlog promote <id> to move a pending backlog item into the active task queue.",
        ]
    )


def cron_usage() -> str:
    return "\n".join(
        [
            "Use /cron every <interval> <request> to schedule recurring work.",
            "Intervals can be like 10m, 2h, or 1d.",
            "Use /cron cancel <id> to cancel a scheduled job.",
            "Use /cron to show scheduled jobs.",
        ]
    )


def evolve_usage() -> str:
    return "\n".join(
        [
            "Use /evolve to show Enoch's self-evolution status.",
            "Use /evolve mode <mode> to set self-evolution behavior.",
            "Modes: disabled, co-evolve, auto-evolve.",
            "Use /evolve theme [text] to show or set the current evolution theme.",
            "Use /evolve brainstorm to generate bounded candidates under the current theme.",
            "Use /evolve list to show current candidates.",
            "Use /evolve approve <id> to approve and queue a candidate as a task.",
            "Use /evolve retry <id> to queue a new task for a failed candidate.",
            "Use /evolve reconcile <id> [backfill] to verify promotion of a completed candidate.",
            "Use /evolve remove <id> [reason] to remove a candidate from future proposals.",
            "Use /evolve schedule <text> to let Enoch interpret common schedule text.",
        ]
    )


def clip_activity_text(text: str, limit: int = 700) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 15].rstrip()} [truncated]"


def clip_activity_block(text: str, limit: int = 700) -> str:
    lines = []
    previous_blank = False
    for raw_line in text.strip().splitlines():
        line = " ".join(raw_line.split())
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    cleaned = "\n".join(lines).strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 15].rstrip()} [truncated]"
