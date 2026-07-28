from __future__ import annotations

import time

from enoch.app.models import WorkStatusMessage
from enoch.providers.contracts import (
    ReviewLandResult,
    ReviewRecord,
)
from enoch.tasks.queue import TaskJob


def format_review_land_result(result: ReviewLandResult) -> str:
    revision = (
        result.revision.display
        if result.revision is not None
        else "reported by the review provider"
    )
    return "\n".join(
        [
            f"Review {result.review.id}: {result.status}.",
            f"Revision: {revision}",
            f"Review result: {result.message or 'No additional detail.'}",
            *(
                [f"URL: {result.review.url}"]
                if result.review.url
                else []
            ),
        ]
    )


def format_open_reviews(
    reviews: tuple[ReviewRecord, ...],
) -> str:
    if not reviews:
        return "Open reviews: none."
    lines = [f"Open reviews ({len(reviews)}):"]
    for review in reviews:
        lines.extend(
            [
                "",
                f"{review.identity.id} [{review_readiness(review)}] "
                f"{review.title or 'Untitled review'}",
                f"Revision: {review.versions[-1].revision.display}",
                review.identity.url,
            ]
        )
    return "\n".join(line for line in lines if line)


def format_review(review: ReviewRecord) -> str:
    lines = [
        f"Review {review.identity.id}",
        f"Title: {review.title or 'Untitled review'}",
        f"Status: {review_readiness(review)}",
        f"State: {review.state or 'unknown'}",
        f"Current revision: {review.versions[-1].revision.display}",
    ]
    if review.landed_revision is not None:
        lines.append(f"Landed revision: {review.landed_revision.display}")
    if review.landed_at:
        lines.append(f"Landed at: {review.landed_at}")
    if review.dependencies:
        lines.append(
            "Depends on: "
            + ", ".join(item.id for item in review.dependencies)
        )
    if review.signals:
        lines.append(
            "Signals: "
            + ", ".join(
                f"{signal.name}={signal.status}"
                for signal in review.signals
            )
        )
    if review.identity.url:
        lines.append(f"URL: {review.identity.url}")
    return "\n".join(lines)


def review_readiness(review: ReviewRecord) -> str:
    if review.state == "landed" or review.landed_revision is not None:
        return "landed"
    if review.state == "closed":
        return "closed"
    if review.draft:
        return "draft"
    blocked = {
        "blocked",
        "conflicting",
        "failed",
        "rejected",
    }
    if any(signal.status in blocked for signal in review.signals):
        return "blocked"
    return "ready" if review.state in {"open", "published"} else review.state


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


def format_task_final_message(
    job: TaskJob,
    final_status: str,
    result: str,
    *,
    task_label: str = "Task",
) -> str:
    summary = job.result or result or "No result summary was recorded."
    if final_status == "paused":
        return "\n".join(
            [
                f"{task_label} #{job.id} paused",
                clip_activity_block(summary, limit=1200),
            ]
        )
    reviews = job.review_urls or ("none",)
    lines = [
        f"{task_label} #{job.id} final update",
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
            "Review URL:",
            *[f"- {review}" for review in reviews],
            "Result summary:",
            clip_activity_block(summary, limit=1200),
        ]
    )
    return "\n".join(lines)


def format_work_status_message(
    status: WorkStatusMessage,
    *,
    task_label: str = "Task",
) -> str:
    elapsed = format_elapsed(max(0, int(time.monotonic() - status.started_at)))
    reviews = status.reviews or ["none"]
    title = (
        f"{task_label} #{status.task_id}"
        if status.task_id is not None
        else ("Work status" if task_label == "Task" else f"{task_label} status")
    )
    lines = [
        title,
        f"Status: {status.status}",
        f"Time: {elapsed}",
        f"Latest update: {status.latest_update}",
        "Reviews published:",
        *[f"- {review}" for review in reviews],
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
            "Intervals stay anchored; missed runs coalesce into one run as soon as Enoch returns.",
            "A schedule keeps at most one task outstanding, and due work goes to the front of the queue.",
            "Use /cron cancel <id> to cancel a scheduled job.",
            "Use /cron to show scheduled jobs.",
        ]
    )


def evolve_usage() -> str:
    return "\n".join(
        [
            "Use /evolve to show the read-only self-evolution dashboard.",
            "Use /evolve evidence [feedback|experience|all] to show recorded evidence.",
            "Use /evolve scan [feedback|experience|all] to scan unprocessed records.",
            "Use /evolve candidates [all] to show candidates.",
            "Use /evolve propose to flush evidence and recommend a candidate.",
            "Use /evolve brainstorm [theme] to generate bounded candidates.",
            "Use /evolve approve <id> to archive a candidate and hand it off to a normal task.",
            "Use /evolve remove <id> [reason] to remove a candidate from future proposals.",
            "Use /evolve config to show settings.",
            "Use /evolve config <mode|theme|feedback-batch|experience-batch|schedule> <value> to change settings.",
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
