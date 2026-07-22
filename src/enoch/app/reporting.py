from __future__ import annotations

from collections import Counter
from pathlib import Path

from enoch.app.presentation import clip_activity_text as _clip_activity_text
from enoch.backlog import BacklogItem, backlog_status
from enoch.cron import CronJob, cron_status, format_cron_interval
from enoch.evolution.core import (
    MODE_AUTO_EVOLVE,
    MODE_DISABLED,
    EvolveCandidate,
    EvolveProposal,
    EvolveReport,
    EvolveState,
    collect_experience_candidates,
    load_evolve_state,
    rank_evolve_candidates,
)
from enoch.evolution.events import EVOLVE_SOURCES, EvolveEvent, load_evolve_events
from enoch.evolution.sources.experience import ExperienceRecord, load_experience_records
from enoch.evolution.sources.feedback import FeedbackSignal, extract_feedback_signals
from enoch.tasks.events import TASK_SOURCES
from enoch.tasks.queue import TaskJob, task_queue_status


def _task_status_message(root: Path) -> str:
    status = task_queue_status(root)
    backlog = backlog_status(root)
    cron = cron_status(root)
    lines = ["Tasks:"]
    if status.running is None:
        lines.append("- running: none")
    else:
        lines.append(f"- running: #{status.running.id} {_clip_activity_text(status.running.text, limit=80)}")
    lines.append(f"- queued: {status.pending_count}")
    lines.append(f"- paused: {status.paused_count}")
    lines.append(f"- backlog: {backlog.pending_count}")
    lines.append(f"- cron: {cron.active_count}")
    return "\n".join(lines)


def _format_tasks_report(root: Path) -> str:
    status = task_queue_status(root)
    backlog = backlog_status(root)
    cron = cron_status(root)
    lines = ["Tasks:"]
    if status.running is None:
        lines.append("Running: none")
    else:
        lines.append(f"Running: {_format_task_list_item(status.running)}")

    lines.append("")
    lines.append("Queued:")
    if status.pending:
        lines.extend(f"- {_format_task_list_item(job)}" for job in status.pending)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Paused:")
    if status.paused:
        lines.extend(f"- {_format_task_list_item(job)}" for job in status.paused)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Recent history:")
    if status.history:
        lines.extend(f"- {_format_task_list_item(job)}" for job in status.history[-10:])
    else:
        lines.append("- none")
    lines.append("")
    lines.append(f"Backlog: {backlog.pending_count}")
    lines.append(f"Cron: {cron.active_count}")
    return "\n".join(lines)


def _format_task_list_item(job: TaskJob) -> str:
    item = f"#{job.id} [{job.status}] {_clip_activity_text(job.text, limit=120)}"
    details = []
    if job.parent_task_id is not None:
        details.append(f"retry of #{job.parent_task_id}")
    if job.pr_urls:
        label = "PR" if len(job.pr_urls) == 1 else "PRs"
        details.append(f"{label}: {', '.join(job.pr_urls)}")
    return f"{item} ({'; '.join(details)})" if details else item


def _format_backlog_report(root: Path) -> str:
    status = backlog_status(root)
    lines = ["Backlog:"]
    lines.append("")
    lines.append("Pending:")
    if status.pending:
        lines.extend(f"- {_format_backlog_list_item(item)}" for item in status.pending)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Recent history:")
    if status.history:
        lines.extend(f"- {_format_backlog_list_item(item)}" for item in status.history[-10:])
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_backlog_list_item(item: BacklogItem) -> str:
    label = f"#{item.id} [{item.priority} {item.status}] {_clip_activity_text(item.text, limit=120)}"
    if item.promoted_task_id is None:
        return label
    return f"{label} (task #{item.promoted_task_id})"


def _format_cron_report(root: Path) -> str:
    status = cron_status(root)
    lines = ["Cron:"]
    lines.append("")
    lines.append("Active:")
    if status.active:
        lines.extend(f"- {_format_cron_list_item(job)}" for job in status.active)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Recent history:")
    if status.history:
        lines.extend(f"- {_format_cron_list_item(job)}" for job in status.history[-10:])
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_cron_list_item(job: CronJob) -> str:
    label = (
        f"#{job.id} [{job.status}] every {format_cron_interval(job.interval_seconds)} "
        f"next {job.next_run_at} {_clip_activity_text(job.text, limit=100)}"
    )
    if job.last_task_id is None:
        return label
    return f"{label} (last task #{job.last_task_id})"


def _format_feedback_report(root: Path) -> str:
    signals = extract_feedback_signals(root)
    lines = ["Feedback:"]
    if not signals:
        lines.append("- none")
        return "\n".join(lines)
    for signal in signals[:20]:
        lines.extend(_format_feedback_signal(signal))
    if len(signals) > 20:
        lines.append(f"- {len(signals) - 20} more")
    return "\n".join(lines)


def _format_feedback_signal(signal: FeedbackSignal) -> list[str]:
    lines = [
        (
            f"- {signal.id} [{signal.kind} x{signal.occurrences}] "
            f"{_clip_activity_text(signal.message, limit=140)}"
        )
    ]
    if signal.last_seen_at:
        lines.append(f"  Last seen: {signal.last_seen_at}")
    return lines


def _format_experience_report(root: Path) -> str:
    state = load_evolve_state(root)
    records = load_experience_records(root, limit=10_000)
    evolve_events = load_evolve_events(root, limit=10_000)
    candidates = rank_evolve_candidates(collect_experience_candidates(root), theme=state.theme)
    lines = ["Experience:", "", "Task statistics:"]
    if records:
        outcomes = Counter(record.outcome for record in records)
        sources = Counter({source: 0 for source in TASK_SOURCES})
        sources.update(record.source for record in records)
        initiators = Counter({"human": 0, "agent": 0})
        initiators.update(record.initiated_by for record in records)
        regressions = [record for record in records if record.regressed]
        completed_tasks = sum(
            record.outcome in {"completed", "regressed", "reverted", "forward-fixed"}
            for record in records
        )
        regression_resolutions = Counter(
            {"unresolved": 0, "reverted": 0, "forward-fixed": 0}
        )
        regression_resolutions.update(
            record.regression_resolution or "unresolved"
            for record in regressions
        )
        regression_sources = Counter({source: 0 for source in TASK_SOURCES})
        regression_sources.update(record.source for record in regressions)
        regression_initiators = Counter({"human": 0, "agent": 0})
        regression_initiators.update(record.initiated_by for record in regressions)
        regression_rate = (
            f"{len(regressions) / completed_tasks:.1%}" if completed_tasks else "0.0%"
        )
        lines.extend(
            [
                f"- Total tasks: {len(records)}",
                f"- Outcomes: {_format_counter(outcomes)}",
                (
                    f"- Regressions: {len(regressions)}/{completed_tasks} completed tasks "
                    f"({regression_rate})"
                ),
                f"- Regression resolution: {_format_counter(regression_resolutions)}",
                f"- Regression sources: {_format_counter(regression_sources)}",
                f"- Regression initiated by: {_format_counter(regression_initiators)}",
                f"- Sources: {_format_counter(sources)}",
                f"- Initiated by: {_format_counter(initiators)}",
            ]
        )
    else:
        lines.append("- none")
    lines.extend(["", "Evolution statistics:"])
    if evolve_events:
        proposals = {
            event.proposal_id: event
            for event in evolve_events
            if event.event == "proposed" and event.proposal_id
        }
        proposal_dispositions = {
            event.proposal_id: event.event
            for event in evolve_events
            if event.proposal_id
            and event.event in {"selected", "removed", "no-action"}
        }
        disposition_counts = Counter(
            {
                "selected": 0,
                "removed": 0,
                "no-action": 0,
                "pending": 0,
                "untracked": 0,
            }
        )
        for proposal_id in proposals:
            if proposal_id.startswith("legacy-proposal-"):
                disposition_counts["untracked"] += 1
            else:
                disposition_counts[
                    proposal_dispositions.get(proposal_id, "pending")
                ] += 1
        tracked_proposals = len(proposals) - disposition_counts["untracked"]
        accepted_proposals = disposition_counts["selected"]
        acceptance_rate = (
            f"{accepted_proposals / tracked_proposals:.1%}"
            if tracked_proposals
            else "0.0%"
        )
        proposal_sources = Counter({source: 0 for source in EVOLVE_SOURCES})
        proposal_sources.update(event.source for event in proposals.values())
        proposal_triggers = Counter(event.trigger or "unknown" for event in proposals.values())
        selected_outcomes = Counter(
            {
                "pending": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "regressed": 0,
                "reverted": 0,
                "forward-fixed": 0,
                "queue-failed": 0,
            }
        )
        for proposal_id, disposition in proposal_dispositions.items():
            if disposition != "selected":
                continue
            proposal_events = [
                event
                for event in evolve_events
                if event.proposal_id == proposal_id
            ]
            outcome = next(
                (
                    event.event
                    for event in reversed(proposal_events)
                    if event.event
                    in {
                        "completed",
                        "failed",
                        "cancelled",
                        "regressed",
                        "reverted",
                        "forward-fixed",
                    }
                ),
                "pending",
            )
            if outcome == "pending" and any(
                event.event == "skipped" and event.reason == "queue-failed"
                for event in proposal_events
            ):
                outcome = "queue-failed"
            selected_outcomes[outcome] += 1
        queued = [event for event in evolve_events if event.event == "queued"]
        outcomes = Counter(
            event.event
            for event in evolve_events
            if event.event
            in {
                "completed",
                "failed",
                "cancelled",
                "regressed",
                "reverted",
                "forward-fixed",
            }
        )
        signal_actors = Counter({"human": 0, "agent": 0, "system": 0})
        signal_actors.update(event.signal_actor for event in queued if event.signal_actor)
        candidate_actors = Counter({"human": 0, "agent": 0, "system": 0})
        candidate_actors.update(event.candidate_actor for event in queued if event.candidate_actor)
        approval_actors = Counter({"human": 0, "agent": 0, "system": 0})
        approval_actors.update(event.approval_actor for event in queued if event.approval_actor)
        autonomous = sum(
            event.event_actor == "system" and event.trigger == "evolve-scheduler"
            for event in queued
        )
        human_approved = sum(
            event.event_actor == "human" and event.trigger == "/evolve approve"
            for event in queued
        )
        lifecycle = Counter({"promoted": 0, "adopted": 0})
        lifecycle.update(
            event.event
            for event in evolve_events
            if event.event in {"promoted", "adopted"}
        )
        lines.extend(
            [
                f"- Checks: {sum(event.event == 'checked' for event in evolve_events)}",
                f"- Proposed: {len(proposals)}",
                f"- Proposal disposition: {_format_counter(disposition_counts)}",
                (
                    f"- Proposal acceptance: {accepted_proposals}/{tracked_proposals} "
                    f"({acceptance_rate})"
                ),
                f"- Proposal sources: {_format_counter(proposal_sources)}",
                f"- Proposal triggers: {_format_counter(proposal_triggers)}",
                f"- Selected proposal outcomes: {_format_counter(selected_outcomes)}",
                (
                    f"- Queued: {len(queued)} "
                    f"(autonomous {autonomous}, human-approved {human_approved})"
                ),
                f"- Queued signal actors: {_format_counter(signal_actors)}",
                f"- Queued candidate actors: {_format_counter(candidate_actors)}",
                f"- Queued approval actors: {_format_counter(approval_actors)}",
                f"- Outcomes: {_format_counter(outcomes)}",
                f"- Governed lifecycle: {_format_counter(lifecycle)}",
            ]
        )
    else:
        lines.append("- none")
    lines.extend(["", "Recent tasks:"])
    if records:
        for record in records[:10]:
            lines.extend(_format_experience_record(record))
    else:
        lines.append("- none")
    lines.extend(["", "Recent evolution events:"])
    if evolve_events:
        for event in evolve_events[-10:][::-1]:
            lines.extend(_format_evolve_event(event))
    else:
        lines.append("- none")
    lines.extend(["", "Current evolve candidates:"])
    if candidates:
        for candidate in candidates[:10]:
            lines.extend(_format_evolve_candidate(candidate))
        if len(candidates) > 10:
            lines.append(f"- {len(candidates) - 10} more")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_experience_record(record: ExperienceRecord) -> list[str]:
    lines = [
        f"- task-{record.task_id} [{record.outcome}] {_clip_activity_text(record.request, limit=120)}",
    ]
    details = [
        f"source {record.source}",
        f"initiated by {record.initiated_by}",
        f"trigger {record.command or 'unknown'}",
    ]
    if record.context_source:
        details.append(f"context {record.context_source}")
    if record.candidate_id:
        details.extend(
            [
                f"evidence {record.evidence_source or record.source}",
                f"signal by {record.signal_actor or 'unknown'}",
                f"candidate by {record.candidate_actor or 'unknown'}",
                f"approved by {record.approval_actor or 'unknown'}",
            ]
        )
    if record.parent_candidate_id:
        details.append(f"parent candidate {record.parent_candidate_id}")
    if record.source_task_id is not None:
        details.append(f"source task-{record.source_task_id}")
    if record.changed_files:
        details.append(f"{len(record.changed_files)} changed file(s)")
    if record.pr_urls:
        details.append(f"{len(record.pr_urls)} PR(s)")
    if record.regressed:
        resolution = record.regression_resolution or "unresolved"
        regression_detail = f"regression {resolution}"
        if record.regression_related_task_id is not None:
            regression_detail += f" by task-{record.regression_related_task_id}"
        details.append(regression_detail)
    lines.append(f"  {'; '.join(details)}")
    if record.result_summary:
        lines.append(f"  Result: {_clip_activity_text(record.result_summary, limit=180)}")
    return lines


def _format_evolve_event(event: EvolveEvent) -> list[str]:
    target = event.candidate_id or "no candidate"
    if event.task_id is not None:
        target += f" -> task-{event.task_id}"
    lines = [f"- {event.event} [{event.event_actor}] {target}"]
    details = [f"trigger {event.trigger or 'unknown'}"]
    if event.proposal_id:
        details.append(f"proposal {_short_proposal_id(event.proposal_id)}")
    if event.source:
        details.append(f"evidence {event.evidence_source or event.source}")
    if event.signal_actor:
        details.append(f"signal by {event.signal_actor}")
    if event.candidate_actor:
        details.append(f"candidate by {event.candidate_actor}")
    if event.approval_actor:
        details.append(f"approved by {event.approval_actor}")
    if event.parent_candidate_id:
        details.append(f"parent candidate {event.parent_candidate_id}")
    if event.source_task_id is not None:
        details.append(f"source task-{event.source_task_id}")
    if event.retry_of_task_id is not None:
        details.append(f"retry of task-{event.retry_of_task_id}")
    if event.mode:
        details.append(f"mode {event.mode}")
    if event.pr_url:
        details.append(f"PR {event.pr_url}")
    if event.merge_commit:
        details.append(f"merge {event.merge_commit[:12]}")
    if event.authoritative_branch:
        details.append(f"authoritative {event.authoritative_branch}")
    if event.version:
        details.append(f"version {event.version[:12]}")
    if event.health_check:
        details.append(f"health {event.health_check}")
    if event.recording_mode:
        details.append(f"recording {event.recording_mode}")
    lines.append(f"  {'; '.join(details)}")
    if event.reason:
        lines.append(f"  Reason: {_clip_activity_text(event.reason, limit=180)}")
    return lines


def _short_proposal_id(proposal_id: str) -> str:
    if proposal_id.startswith("proposal-"):
        return proposal_id[:21]
    return proposal_id


def _format_counter(counts: Counter[str]) -> str:
    return ", ".join(f"{key} {counts[key]}" for key in sorted(counts)) or "none"


def _evolve_check_reason(proposal: EvolveProposal) -> str:
    parts = [f"ranked-{len(proposal.candidates)}-candidate(s)"]
    if proposal.brainstorm_attempted:
        parts.append(f"fallback-brainstorm-added-{proposal.brainstorm_added}")
    elif proposal.brainstorm_skip_reason:
        parts.append(f"fallback-{proposal.brainstorm_skip_reason}")
    if proposal.brainstorm_error:
        parts.append(f"fallback-error-{proposal.brainstorm_error}")
    return "; ".join(parts)


def _evolve_skip_reason(proposal: EvolveProposal) -> str:
    if proposal.brainstorm_error:
        return f"brainstorm-failed: {proposal.brainstorm_error}"
    if proposal.brainstorm_skip_reason:
        return proposal.brainstorm_skip_reason
    if proposal.brainstorm_attempted:
        return "no-candidate-after-brainstorm"
    return "no-candidate"


def _format_evolve_proposal(proposal: EvolveProposal) -> str:
    report = proposal.report
    if report.state.mode == MODE_DISABLED:
        return "Evolve is disabled. Use /evolve mode co-evolve or /evolve mode auto-evolve before proposing."
    candidate = proposal.top_candidate
    if candidate is None:
        if proposal.brainstorm_skip_reason == "candidate-running":
            return "Enoch found no new evolve candidate because evolve work is already running."
        if proposal.brainstorm_skip_reason == "theme-not-set":
            return "Enoch found no new evolve candidate. Set a theme with /evolve theme <text> to enable fallback brainstorming."
        if proposal.brainstorm_skip_reason == "cooldown":
            return "Enoch found no new evolve candidate. Fallback brainstorming for this theme is on a 24-hour cooldown."
        if proposal.brainstorm_error:
            return f"Enoch found no new evolve candidate. Fallback brainstorming failed: {proposal.brainstorm_error}"
        if proposal.brainstorm_attempted:
            return "Enoch found no new evolve candidate after fallback brainstorming."
        return "Enoch found no new evolve candidate to propose."
    lines = [
        "Enoch proposes:",
        f"Theme: {report.state.theme or 'not set'}",
        f"Ranked {len(proposal.candidates)} actionable candidate(s) from the six evolve sources.",
    ]
    if proposal.brainstorm_attempted:
        lines.append(f"Fallback brainstorm added {proposal.brainstorm_added} candidate(s).")
    lines.append("")
    lines.extend(_format_evolve_candidate(candidate))
    lines.append("")
    if candidate.status == "failed":
        lines.append(f"Retry with /evolve retry {candidate.id}.")
    else:
        lines.append(f"Approve with /evolve approve {candidate.id}.")
    lines.append(f"Remove with /evolve remove {candidate.id}.")
    return "\n".join(lines)


def _format_evolve_report(report: EvolveReport) -> str:
    state = report.state
    lines = [
        "Evolve:",
        f"Mode: {state.mode}",
        f"Theme: {state.theme or 'not set'}",
        f"Schedule: {_format_evolve_schedule(state)}",
        "",
        "Candidate counts:",
    ]
    if report.counts_by_source:
        for source in sorted(report.counts_by_source):
            lines.append(f"- {source}: {report.counts_by_source[source]}")
    else:
        lines.append("- none")
    lines.extend(["", "Top candidate:"])
    if report.top_candidate is None:
        lines.append("- none")
    else:
        lines.extend(_format_evolve_candidate(report.top_candidate))
    lines.extend(["", f"Next action: {_evolve_next_action(report)}"])
    return "\n".join(lines)


def _format_evolve_schedule(state: EvolveState) -> str:
    if not state.schedule_enabled or state.schedule_interval_seconds <= 0:
        return "off"
    next_run = state.schedule_next_run_at or "unknown"
    last_run = f"; last {state.schedule_last_run_at}" if state.schedule_last_run_at else ""
    if state.schedule_daily_time:
        return f"daily {state.schedule_daily_time}; next {next_run}{last_run}"
    if state.schedule_cron_expression:
        return f"cron {state.schedule_cron_expression}; next {next_run}{last_run}"
    return f"every {format_cron_interval(state.schedule_interval_seconds)}; next {next_run}{last_run}"


def _format_evolve_theme(state: EvolveState) -> str:
    return "\n".join(
        [
            "Evolve theme:",
            state.theme or "not set",
            "",
            "Set with /evolve theme <text>.",
        ]
    )


def _format_evolve_candidate(candidate: EvolveCandidate) -> list[str]:
    return [
        f"- {candidate.id} [{candidate.status} {candidate.source}] {_clip_activity_text(candidate.title, limit=100)}",
        (
            f"  Provenance: evidence {candidate.evidence_source or candidate.source}; "
            f"signal by {candidate.signal_actor}; candidate by {candidate.candidate_actor}"
        ),
        f"  Score: {candidate.score}",
        f"  Rationale: {_clip_activity_text(candidate.rationale, limit=180)}",
        f"  Proposed change: {_clip_activity_text(candidate.proposed_change, limit=180)}",
        f"  Test plan: {_clip_activity_text(candidate.test_plan, limit=180)}",
    ]


def _format_evolve_candidates(candidates: tuple[EvolveCandidate, ...], *, include_inactive: bool = False) -> str:
    title = "Evolve candidates"
    if include_inactive:
        title += " (all)"
    lines = [f"{title}:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)
    for candidate in candidates[:10]:
        lines.extend(_format_evolve_candidate(candidate))
    if len(candidates) > 10:
        lines.append(f"- {len(candidates) - 10} more")
    return "\n".join(lines)


def _evolve_next_action(report: EvolveReport) -> str:
    if report.state.mode == MODE_DISABLED:
        return "disabled; Enoch will not collect or rank self-evolution candidates."
    if report.top_candidate is None:
        return "no candidate yet."
    if report.top_candidate.status == "failed":
        return "propose retrying this failed candidate and wait for explicit human approval."
    if report.state.mode == MODE_AUTO_EVOLVE:
        return "select this bounded candidate, then queue or run work only after guardrails pass."
    return "propose this candidate and wait for human approval before changing code."
