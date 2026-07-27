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
    load_evolve_candidates,
    load_evolve_state,
    rank_evolve_candidates,
)
from enoch.evolution.evidence import (
    EvidenceScanResult,
    EvidenceSignal,
    load_evidence,
    load_evidence_settings,
    pending_evidence_counts,
)
from enoch.evolution.events import EVOLVE_SOURCES, EvolveEvent, load_evolve_events
from enoch.evolution.sources.experience import ExperienceRecord, load_experience_records
from enoch.tasks.events import TASK_SOURCES
from enoch.tasks.queue import TaskJob, TaskQueueStatus, task_queue_status


def _task_status_message(
    root: Path,
    *,
    task_status: TaskQueueStatus | None = None,
) -> str:
    status = task_status or task_queue_status(root)
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


def _format_tasks_report(
    root: Path,
    *,
    task_status: TaskQueueStatus | None = None,
) -> str:
    status = task_status or task_queue_status(root)
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
    if job.review_urls:
        label = "Review" if len(job.review_urls) == 1 else "Reviews"
        details.append(f"{label}: {', '.join(job.review_urls)}")
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
    return _format_evidence_report(root, source="feedback")


def _format_evidence_report(root: Path, *, source: str = "all") -> str:
    normalized_source = source.strip().lower() or "all"
    wanted_source = normalized_source if normalized_source in {"feedback", "experience"} else ""
    signals = load_evidence(
        root,
        source=wanted_source,
        include_inactive=True,
    )
    pending = pending_evidence_counts(root)
    settings = load_evidence_settings(root)
    title = "Evolution evidence" + (
        f" ({wanted_source})" if wanted_source else ""
    )
    lines = [
        f"{title}:",
        (
            f"Pending: feedback {pending['feedback']}/{settings.feedback_batch_size}, "
            f"experience {pending['experience']}/{settings.experience_batch_size}"
        ),
        "",
        "Recorded evidence:",
    ]
    if not signals:
        lines.append("- none")
        return "\n".join(lines)
    for signal in reversed(signals[-20:]):
        lines.extend(_format_evidence_signal(signal))
    if len(signals) > 20:
        lines.append(f"- {len(signals) - 20} older evidence item(s)")
    return "\n".join(lines)


def _format_evidence_signal(signal: EvidenceSignal) -> list[str]:
    lines = [
        (
            f"- {signal.id} [{signal.status} {signal.source}; "
            f"confidence {signal.confidence:.0%}] "
            f"{_clip_activity_text(signal.observation, limit=180)}"
        ),
        (
            f"  Area: {_clip_activity_text(signal.affected_area, limit=100)}; "
            f"type: {_clip_activity_text(signal.evidence_type, limit=80)}; "
            f"explicit: {'yes' if signal.explicit else 'no'}"
        ),
        f"  Desired outcome: {_clip_activity_text(signal.desired_outcome, limit=180)}",
        f"  Refs: {', '.join(signal.evidence_refs) or 'none'}",
    ]
    if signal.candidate_ids:
        lines.append(f"  Candidates: {', '.join(signal.candidate_ids)}")
    return lines


def _format_evidence_scan_results(
    results: tuple[EvidenceScanResult, ...],
) -> str:
    lines = ["Evidence scan:"]
    for result in results:
        if result.status == "completed":
            lines.append(
                f"- {result.source}: processed {result.processed}; "
                f"recorded {len(result.evidence)} evidence item(s); "
                f"pending {result.remaining}"
            )
        elif result.status == "waiting":
            lines.append(
                f"- {result.source}: waiting; {result.remaining} pending record(s)"
            )
        elif result.status == "empty":
            lines.append(f"- {result.source}: no unscanned records")
        else:
            lines.append(
                f"- {result.source}: failed; inputs remain pending"
                + (f" ({_clip_activity_text(result.error, limit=180)})" if result.error else "")
            )
    return "\n".join(lines)


def _format_evolve_config(report: EvolveReport) -> str:
    settings = report.evidence_settings
    return "\n".join(
        [
            "Evolve config:",
            f"- Mode: {report.state.mode}",
            f"- Theme: {report.state.theme or 'not set'}",
            f"- Feedback batch: {settings.feedback_batch_size} user messages",
            f"- Experience batch: {settings.experience_batch_size} changed task IDs",
            f"- Schedule: {_format_evolve_schedule(report.state)}",
            "",
            "Set with /evolve config <mode|theme|feedback-batch|experience-batch|schedule> <value>.",
        ]
    )


def _format_experience_report(root: Path) -> str:
    state = load_evolve_state(root)
    records = load_experience_records(root, limit=10_000)
    evolve_events = load_evolve_events(root, limit=10_000)
    candidates = rank_evolve_candidates(
        (
            candidate
            for candidate in load_evolve_candidates(
                root,
                include_inactive=True,
                theme=state.theme,
            )
            if candidate.source == "experience"
        ),
        theme=state.theme,
    )
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
        handoffs = [event for event in evolve_events if event.event == "queued"]
        signal_actors = Counter({"human": 0, "agent": 0, "system": 0})
        signal_actors.update(event.signal_actor for event in handoffs if event.signal_actor)
        candidate_actors = Counter({"human": 0, "agent": 0, "system": 0})
        candidate_actors.update(event.candidate_actor for event in handoffs if event.candidate_actor)
        approval_actors = Counter({"human": 0, "agent": 0, "system": 0})
        approval_actors.update(event.approval_actor for event in handoffs if event.approval_actor)
        autonomous = sum(
            event.event_actor == "system" and event.trigger == "evolve-scheduler"
            for event in handoffs
        )
        human_approved = sum(
            event.event_actor == "human" and event.trigger == "/evolve approve"
            for event in handoffs
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
                (
                    f"- Task handoffs: {len(handoffs)} "
                    f"(autonomous {autonomous}, human-approved {human_approved})"
                ),
                f"- Handoff signal actors: {_format_counter(signal_actors)}",
                f"- Handoff candidate actors: {_format_counter(candidate_actors)}",
                f"- Handoff approval actors: {_format_counter(approval_actors)}",
                "- Post-handoff outcomes are owned by the task journal.",
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
    if event.review_id:
        details.append(f"review {event.review_id}")
    if event.revision_id:
        details.append(f"landed {event.revision_id[:12]}")
    if event.authoritative_revision_id:
        details.append(
            f"authoritative revision {event.authoritative_revision_id[:12]}"
        )
    if event.authoritative_name:
        details.append(f"authoritative target {event.authoritative_name}")
    if event.version:
        details.append(f"version {event.version[:12]}")
    if event.health_check:
        details.append(f"health {event.health_check}")
    if event.recording_mode:
        details.append(f"recording {event.recording_mode}")
    if event.removal_classification:
        details.append(f"classification {event.removal_classification}")
    if event.evidence_refs:
        details.append(f"refs {', '.join(event.evidence_refs)}")
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
    parts = [f"pre-ranked-{len(proposal.candidates)}-candidate(s)"]
    if proposal.curation is not None:
        parts.append(proposal.curation.status)
        parts.append(f"curation-{proposal.curation.id}")
    if proposal.scheduled_brainstorm_status:
        parts.append(
            f"scheduled-brainstorm-{proposal.scheduled_brainstorm_status}"
        )
    if proposal.scheduled_brainstorm_error:
        parts.append(
            f"scheduled-brainstorm-error-{proposal.scheduled_brainstorm_error}"
        )
    return "; ".join(parts)


def _evolve_skip_reason(proposal: EvolveProposal) -> str:
    if proposal.curation is not None and proposal.curation.status == "llm":
        if proposal.curation.remove_suggestions:
            return "curation-suggestions-only"
    if proposal.scheduled_brainstorm_error:
        return (
            "scheduled-brainstorm-failed: "
            f"{proposal.scheduled_brainstorm_error}"
        )
    if proposal.scheduled_brainstorm_status:
        return f"scheduled-brainstorm-{proposal.scheduled_brainstorm_status}"
    return "no-candidate"


def _format_evolve_proposal(proposal: EvolveProposal) -> str:
    report = proposal.report
    if report.state.mode == MODE_DISABLED:
        return (
            "Evolve is disabled. Use /evolve config mode co-evolve or "
            "/evolve config mode auto-evolve before proposing."
        )
    candidate = proposal.top_candidate
    curation = proposal.curation
    if candidate is None and not (
        curation is not None and curation.remove_suggestions
    ):
        message = "Enoch found no new evolve candidate to propose."
        activity = _format_proposal_activity(proposal)
        return message + (f"\n\n{activity}" if activity else "")
    lines = [
        "Enoch proposes:",
        f"Theme: {report.state.theme or 'not set'}",
        f"Ranked {len(proposal.candidates)} actionable candidate(s) from the evolution pathways.",
        "Deterministic ranking was used only for bounded input ordering and fallback.",
    ]
    proposal_activity = _format_proposal_activity(proposal)
    if proposal_activity:
        lines.extend(["", proposal_activity])
    lines.append("")
    if curation is not None and curation.status == "llm":
        lines.append("LLM recommended candidate:")
    else:
        reason = curation.fallback_reason if curation is not None else "curator-unavailable"
        lines.append(f"Deterministic fallback recommendation ({reason}):")
    if candidate is None:
        lines.append("- none")
    else:
        lines.extend(_format_evolve_candidate(candidate))
        if curation is not None and curation.recommendation is not None:
            recommendation = curation.recommendation
            lines.extend(
                [
                    f"  Curation reason: {_clip_activity_text(recommendation.reason, limit=180)}",
                    f"  Scope guidance: {_clip_activity_text(recommendation.scope_guidance, limit=180)}",
                    f"  Risk guidance: {_clip_activity_text(recommendation.risk_guidance, limit=180)}",
                    f"  Test guidance: {_clip_activity_text(recommendation.test_plan_guidance, limit=180)}",
                ]
            )
        lines.append("")
        lines.append(f"Approve with /evolve approve {candidate.id}.")
        lines.append(f"Remove with /evolve remove {candidate.id}.")
    if curation is not None and curation.remove_suggestions:
        lines.extend(["", "LLM remove suggestions (no status changed):"])
        for suggestion in curation.remove_suggestions:
            lines.append(
                f"- {suggestion.candidate_id} [{suggestion.classification}] "
                f"{_clip_activity_text(suggestion.reason, limit=180)}"
            )
            if suggestion.evidence_refs:
                lines.append(
                    "  Evidence: "
                    + ", ".join(suggestion.evidence_refs)
                )
            lines.append(
                f"  Human action: /evolve remove {suggestion.candidate_id} {suggestion.classification}"
            )
    lines.extend(["", "No recommendation or remove suggestion changes state without human action."])
    return "\n".join(lines)


def _format_proposal_activity(proposal: EvolveProposal) -> str:
    lines: list[str] = []
    if proposal.evidence_scan_results:
        lines.append(_format_evidence_scan_results(proposal.evidence_scan_results))
    if proposal.evidence_candidates_added:
        lines.append(
            f"Evidence synthesis added {proposal.evidence_candidates_added} candidate(s)."
        )
    if proposal.evidence_synthesis_error:
        lines.append(
            "Evidence synthesis failed without consuming evidence: "
            f"{_clip_activity_text(proposal.evidence_synthesis_error, limit=180)}"
        )
    brainstorm = _format_scheduled_brainstorm(proposal)
    if brainstorm:
        lines.append(brainstorm)
    return "\n".join(lines)


def _format_scheduled_brainstorm(proposal: EvolveProposal) -> str:
    status = proposal.scheduled_brainstorm_status
    if not status:
        return ""
    if status == "created":
        detail = (
            f"created {proposal.scheduled_brainstorm_created} candidate(s)"
        )
        if proposal.scheduled_brainstorm_existing:
            detail += (
                f"; skipped {proposal.scheduled_brainstorm_existing} existing"
            )
        return f"Scheduled brainstorming: {detail}."
    if status == "existing":
        return (
            "Scheduled brainstorming: all returned candidates already exist."
        )
    if status == "no-ideas":
        return (
            "Scheduled brainstorming: no sufficiently novel bounded idea found."
        )
    if status == "cooldown":
        return (
            "Scheduled brainstorming: 24-hour theme cooldown is active."
        )
    if status == "theme-not-set":
        return "Scheduled brainstorming: skipped because no theme is set."
    if status == "explicit-only":
        return (
            "Scheduled brainstorming: disabled in co-evolve mode; "
            "use /evolve brainstorm explicitly."
        )
    if status == "not-needed":
        return (
            "Scheduled brainstorming: not needed because candidates already exist."
        )
    if status == "failed":
        return (
            "Scheduled brainstorming failed: "
            f"{_clip_activity_text(proposal.scheduled_brainstorm_error, limit=180)}"
        )
    return f"Scheduled brainstorming: {status}."


def _format_evolve_report(report: EvolveReport) -> str:
    state = report.state
    lines = [
        "Evolve:",
        f"Mode: {state.mode}",
        f"Theme: {state.theme or 'not set'}",
        f"Schedule: {_format_evolve_schedule(state)}",
        "",
        "Evidence:",
        (
            f"- feedback: {report.evidence_counts.get('feedback', 0)} recorded; "
            f"{report.pending_evidence.get('feedback', 0)}/"
            f"{report.evidence_settings.feedback_batch_size} pending"
        ),
        (
            f"- experience: {report.evidence_counts.get('experience', 0)} recorded; "
            f"{report.pending_evidence.get('experience', 0)}/"
            f"{report.evidence_settings.experience_batch_size} pending task IDs"
        ),
        "",
        "Candidate counts:",
    ]
    if report.counts_by_source:
        for source in sorted(report.counts_by_source):
            lines.append(f"- {source}: {report.counts_by_source[source]}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Top candidate:",
            "(deterministic pre-ranking; semantic selection occurs in /evolve propose)",
        ]
    )
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
            "Set with /evolve config theme <text>.",
        ]
    )


def _format_evolve_candidate(candidate: EvolveCandidate) -> list[str]:
    lines = [
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
    if candidate.evidence_ids:
        lines.append(f"  Evidence IDs: {', '.join(candidate.evidence_ids)}")
    if candidate.evidence_refs:
        lines.append(f"  Evidence refs: {', '.join(candidate.evidence_refs)}")
    if candidate.source_url:
        lines.append(f"  Source: {candidate.source_url}")
    elif candidate.source_repository:
        revision = f"@{candidate.source_revision}" if candidate.source_revision else ""
        location = f":{candidate.source_path}" if candidate.source_path else ""
        lines.append(
            f"  Source: {candidate.source_repository}{revision}{location}"
        )
    if candidate.source_theme:
        lines.append(f"  Brainstorm theme: {candidate.source_theme}")
    if candidate.source_context_hash:
        lines.append(
            f"  Brainstorm context: {candidate.source_context_hash[:12]}"
        )
    return lines


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
    if report.state.mode == MODE_AUTO_EVOLVE:
        return "propose this candidate and wait for explicit human approval; scheduling does not queue it."
    return "propose this candidate and wait for human approval before changing code."
