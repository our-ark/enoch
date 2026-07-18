from __future__ import annotations

from enoch.github.workflow import (
    LocalPublishResult,
    PullRequestResult,
    RemotePublishResult,
)
from enoch.immune import ImmuneResult


SUMMARY_CLIP_CHARS = 2000


def summarize_for_log(text: str, limit: int = SUMMARY_CLIP_CHARS) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}\n\n[truncated]"


def format_doctor_result(result: ImmuneResult) -> str:
    status = "passed" if result.passed else "failed"
    lines = [f"Doctor {status}."]
    checks = getattr(result, "checks", None)
    if isinstance(checks, list):
        lines.extend(_doctor_check_sections(checks))
    else:
        lines.append(f"Command: {result.command}")
    lines.extend(
        [
            f"Diagnosis: {result.diagnosis.summary}",
            f"Suggested next action: {result.diagnosis.suggested_action}",
        ]
    )
    if result.diagnosis.failing_tests:
        lines.append("Failing tests:")
        lines.extend(f"- {test}" for test in result.diagnosis.failing_tests)
    if result.diagnosis.likely_files:
        lines.append("Likely files:")
        lines.extend(f"- {path}" for path in result.diagnosis.likely_files)
    for check in checks if isinstance(checks, list) else []:
        if check.passed:
            continue
        lines.append(f"Failed check: {check.name}")
        lines.append(f"Check command: {check.command}")
        if check.output:
            lines.append("Check output:")
            lines.append(doctor_output_excerpt(check.output))
    return "\n".join(lines)


def _doctor_check_sections(checks: list[object]) -> list[str]:
    categories = [
        ("code health", "Code health:"),
        ("operational readiness", "Operational readiness:"),
    ]
    lines = []
    for category, heading in categories:
        category_checks = [check for check in checks if getattr(check, "category", "code health") == category]
        if not category_checks:
            continue
        lines.append("")
        lines.append(heading)
        for check in category_checks:
            status = "passed" if check.passed else "failed"
            summary = getattr(check, "summary", "")
            suffix = f" ({summary})" if summary else ""
            lines.append(f"- {check.name}: {status}{suffix}")
    return lines


def doctor_output_excerpt(output: str, limit: int = SUMMARY_CLIP_CHARS) -> str:
    cleaned = output.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}\n\n[truncated]"


def format_telegram_publish_result(result: LocalPublishResult) -> str:
    files = "\n".join(f"- {path}" for path in result.changed_files)
    return "\n".join(
        [
            "Enoch committed this change.",
            f"Branch: {result.branch}",
            f"Commit: {result.commit_sha} {result.commit_message}",
            "Files:",
            files,
            f"Doctor: {'passed' if result.doctor.passed else 'failed'}",
            f"Diagnosis: {result.doctor.diagnosis.summary}",
            "Publish step: local commit created.",
        ]
    )


def format_telegram_remote_publish_result(result: RemotePublishResult) -> str:
    return "\n".join(
        [
            "Enoch pushed this branch.",
            f"Branch: {result.branch}",
            f"Remote: {result.remote}",
            f"Commits pushed: {result.ahead_count}",
            f"Review URL: {result.compare_url or 'unavailable'}",
            "Publish step: branch pushed to GitHub.",
        ]
    )


def format_pr_result(result: PullRequestResult) -> str:
    lines = []
    if result.created:
        lines.append("Enoch opened a pull request.")
        lines.append(f"PR URL: {result.url or 'unavailable'}")
    else:
        lines.append("Enoch could not open a pull request automatically.")
        if result.note:
            lines.append(f"Reason: {result.note}")
        if result.fallback_url:
            lines.append(f"Review URL: {result.fallback_url}")
    lines.extend(
        [
            f"Branch: {result.branch}",
            f"Title: {result.title}",
            "Local checkout will return to its resident branch after the GitHub handoff.",
        ]
    )
    return "\n".join(lines)


def pr_step_update(result: PullRequestResult) -> str:
    if result.created:
        return f"Opened pull request: {result.url or 'URL unavailable'}"
    if result.fallback_url:
        return f"Could not open a pull request automatically. Review URL: {result.fallback_url}"
    return "Could not open a pull request automatically."


def publish_summary(result: LocalPublishResult, *, title: str = "Committed local change") -> str:
    files = "\n".join(f"- {path}" for path in result.changed_files)
    lines = [
        title,
        "",
        f"Branch: {result.branch}",
        f"Commit: {result.commit_sha} {result.commit_message}",
        "Files:",
        files,
        f"Doctor: {'passed' if result.doctor.passed else 'failed'}",
        f"Diagnosis: {result.doctor.diagnosis.summary}",
    ]
    return "\n".join(lines)


def remote_publish_summary(result: RemotePublishResult) -> str:
    return "\n".join(
        [
            "Pushed branch",
            "",
            f"Branch: {result.branch}",
            f"Remote: {result.remote}",
            f"Commits pushed: {result.ahead_count}",
            f"Review URL: {result.compare_url or 'unavailable'}",
        ]
    )


def pr_summary(result: PullRequestResult) -> str:
    return "\n".join(
        [
            pr_summary_title(result),
            "",
            f"Branch: {result.branch}",
            f"Title: {result.title}",
            f"Created: {result.created}",
            f"PR URL: {result.url or 'unavailable'}",
            f"Review URL: {result.fallback_url or 'unavailable'}",
            f"Note: {result.note or 'none'}",
        ]
    )


def pr_summary_title(result: PullRequestResult) -> str:
    if not result.created:
        return "Prepared pull request fallback"
    return "Opened pull request"
