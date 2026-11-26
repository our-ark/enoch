"""Automatic repair of red CI on a review Enoch is landing or watching.

The pending-merge watcher (``enoch.app.pending_merges``) uses these pieces when
a review's CI fails:

- ``classify_check_failures`` separates infrastructure failures (runner or
  setup errors, cancelled or timed-out jobs, network errors with no test
  failure in the log), which are re-run once, from failures that need a fix.
- ``ci_fix_task_brief`` is the request for a fix task that works on the
  review's own branch, modeled on the manual brief that first fixed CI this way.
- The fix task reports what it did in one ``[ENOCH_CI_FIX_RESULT]`` block.
  ``extract_ci_fix_results`` strips it from the visible reply and
  ``record_ci_fix_result`` stores it in private state, keyed by task id, so
  the watcher can accept exactly the head that task pushed.

Everything here is provider-agnostic; the forge supplies failure evidence via
the optional ``forge.ci-repair`` operations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re

from enoch.paths import private_state_path
from enoch.providers.contracts import ReviewCheckFailure
from enoch.state import atomic_write, load_json_object


CI_FIX_RESULTS_STATE_FILE = "ci_fix_results.json"
CI_FIX_RESULT_TAG = "ENOCH_CI_FIX_RESULT"
CI_FIX_OUTCOMES = frozenset({"pushed", "main-failure", "no-change"})
# Task states after which a fix task will not push anything more.
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled", "regressed"})

_SIGNAL = re.compile(
    rf"\[{CI_FIX_RESULT_TAG}\]\s*(.*?)\s*\[/{CI_FIX_RESULT_TAG}\]",
    re.DOTALL,
)
# Opaque revision ids: non-empty, no whitespace.
_REVISION = re.compile(r"^\S{1,128}$")

# Conclusions that mean the job did not get to judge the code.
_INFRASTRUCTURE_CONCLUSIONS = frozenset(
    {"cancelled", "timed_out", "startup_failure", "stale", "skipped_by_runner"}
)
# Evidence that tests ran and failed: the change needs a fix, not a re-run.
_TEST_FAILURE_MARKERS = (
    re.compile(r"\bFAIL: test"),
    re.compile(r"\bERROR: test"),
    re.compile(r"FAILED \((?:failures|errors)="),
    re.compile(r"\bFAILED [\w./-]+::"),
    re.compile(r"::[\w\[\]-]+ FAILED\b"),
    re.compile(r"=+ .*\b\d+ failed\b"),
    re.compile(r"\bAssertionError\b"),
    re.compile(r"\bSyntaxError\b"),
    re.compile(r"\bImportError\b|\bModuleNotFoundError\b"),
)
# Runner, setup and network errors that say nothing about the code.
_INFRASTRUCTURE_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"runner has received a shutdown signal",
        r"lost communication with the server",
        r"hosted runner encountered an error",
        r"the operation was canceled",
        r"no space left on device",
        r"could not resolve host",
        r"temporary failure in name resolution",
        r"name or service not known",
        r"connection (?:reset|refused|timed out)",
        r"\bECONNRESET\b|\bETIMEDOUT\b|\bEAI_AGAIN\b",
        r"read ?timed ?out|ReadTimeoutError",
        r"\b(?:502|503|504) (?:Bad Gateway|Service Unavailable|Gateway Time-?out)\b",
        r"HTTP Error 5\d\d",
        r"failed to download action",
        r"unable to download artifact",
        r"API rate limit exceeded",
        r"fatal: unable to access",
        r"error: RPC failed",
    )
)


@dataclass(frozen=True)
class FailureClassification:
    infrastructure: bool
    reason: str


def classify_check_failures(
    failures: tuple[ReviewCheckFailure, ...],
) -> FailureClassification:
    """Return infrastructure only when every failure looks like infrastructure.

    A failure with an unreadable log only counts as infrastructure when its
    conclusion (cancelled, timed out, ...) already says the job never judged
    the code. Any sign of a failing test means it needs a fix.
    """

    if not failures:
        return FailureClassification(False, "no failure evidence")
    reasons = []
    for failure in failures:
        log = failure.log or ""
        if any(marker.search(log) for marker in _TEST_FAILURE_MARKERS):
            return FailureClassification(False, f"{failure.name}: failing tests in the log")
        if failure.detail.lower() in _INFRASTRUCTURE_CONCLUSIONS:
            reasons.append(f"{failure.name}: {failure.detail.lower().replace('_', ' ')}")
            continue
        if any(marker.search(log) for marker in _INFRASTRUCTURE_MARKERS):
            reasons.append(f"{failure.name}: runner, setup or network error")
            continue
        return FailureClassification(False, f"{failure.name}: no infrastructure cause found")
    return FailureClassification(True, "; ".join(reasons))


# Fix attempts ---------------------------------------------------------------


@dataclass(frozen=True)
class FixCommit:
    revision: str
    subject: str = ""

    @property
    def short(self) -> str:
        return self.revision[:7]


@dataclass(frozen=True)
class FixAttempt:
    """One automatic fix task for a red head.

    ``state`` is ``active`` while the task may still push, then ``pushed`` (its
    head was accepted), ``main-failure`` (the failure reproduces on the base and
    the task opened a separate review), or ``no-push``.
    """

    task_id: int
    head: str
    checks: tuple[str, ...]
    started_at: str
    state: str = "active"
    pushed_head: str = ""
    review_url: str = ""
    commits: tuple[FixCommit, ...] = ()

    @property
    def active(self) -> bool:
        return self.state == "active"


@dataclass(frozen=True)
class CiRepair:
    """Repair bookkeeping shared by pending merges and watched reviews.

    ``reruns`` lists heads whose failed jobs were re-run (once per head).
    ``paused_seconds`` accumulates time with a fix task queued or running, and
    ``pause_started_at`` is set while one is; the merge timeout excludes both.
    """

    attempts: tuple[FixAttempt, ...] = ()
    reruns: tuple[str, ...] = ()
    paused_seconds: float = 0.0
    pause_started_at: str = ""

    @property
    def active_attempt(self) -> FixAttempt | None:
        for attempt in reversed(self.attempts):
            if attempt.active:
                return attempt
        return None

    @property
    def task_ids(self) -> tuple[int, ...]:
        return tuple(attempt.task_id for attempt in self.attempts)

    @property
    def fix_commits(self) -> tuple[tuple[int, tuple[FixCommit, ...]], ...]:
        return tuple(
            (attempt.task_id, attempt.commits)
            for attempt in self.attempts
            if attempt.state == "pushed" and attempt.commits
        )

    def replace_attempt(self, attempt: FixAttempt) -> "CiRepair":
        return replace(
            self,
            attempts=tuple(
                attempt if item.task_id == attempt.task_id else item
                for item in self.attempts
            ),
        )


def ci_repair_to_json(repair: CiRepair) -> dict[str, object]:
    return {
        "attempts": [
            {
                "task_id": attempt.task_id,
                "head": attempt.head,
                "checks": list(attempt.checks),
                "started_at": attempt.started_at,
                "state": attempt.state,
                "pushed_head": attempt.pushed_head,
                "review_url": attempt.review_url,
                "commits": [
                    {"revision": commit.revision, "subject": commit.subject}
                    for commit in attempt.commits
                ],
            }
            for attempt in repair.attempts
        ],
        "reruns": list(repair.reruns),
        "paused_seconds": repair.paused_seconds,
        "pause_started_at": repair.pause_started_at,
    }


def ci_repair_from_json(raw: object) -> CiRepair:
    if not isinstance(raw, dict):
        return CiRepair()
    attempts = []
    for item in raw.get("attempts") if isinstance(raw.get("attempts"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            task_id = int(item.get("task_id") or 0)
        except (TypeError, ValueError):
            continue
        if task_id <= 0:
            continue
        commits = tuple(
            FixCommit(
                revision=str(commit.get("revision") or "").strip(),
                subject=str(commit.get("subject") or "").strip(),
            )
            for commit in (item.get("commits") if isinstance(item.get("commits"), list) else [])
            if isinstance(commit, dict) and str(commit.get("revision") or "").strip()
        )
        checks = item.get("checks")
        attempts.append(
            FixAttempt(
                task_id=task_id,
                head=str(item.get("head") or "").strip(),
                checks=tuple(
                    str(check) for check in (checks if isinstance(checks, list) else [])
                ),
                started_at=str(item.get("started_at") or "").strip(),
                state=str(item.get("state") or "active").strip() or "active",
                pushed_head=str(item.get("pushed_head") or "").strip(),
                review_url=str(item.get("review_url") or "").strip(),
                commits=commits,
            )
        )
    reruns = raw.get("reruns")
    try:
        paused = max(0.0, float(raw.get("paused_seconds") or 0.0))
    except (TypeError, ValueError):
        paused = 0.0
    return CiRepair(
        attempts=tuple(attempts),
        reruns=tuple(
            str(head).strip()
            for head in (reruns if isinstance(reruns, list) else [])
            if str(head).strip()
        ),
        paused_seconds=paused,
        pause_started_at=str(raw.get("pause_started_at") or "").strip(),
    )


def format_fix_commits(repair: CiRepair) -> str:
    """``+1 fix commit by task #61: abc1234 Subject`` (empty when there are none)."""

    groups = repair.fix_commits
    total = sum(len(commits) for _, commits in groups)
    if not total:
        return ""
    parts = []
    for task_id, commits in groups:
        listed = ", ".join(
            f"{commit.short} {commit.subject}".strip() for commit in commits
        )
        parts.append(f"by task #{task_id}: {listed}")
    noun = "fix commit" if total == 1 else "fix commits"
    return f"+{total} {noun} " + "; ".join(parts)


# Fix task brief ---------------------------------------------------------------


def ci_fix_task_brief(
    *,
    number: int,
    url: str,
    head: str,
    base_branch: str,
    failures: tuple[ReviewCheckFailure, ...],
    check_names: tuple[str, ...],
    goal: str,
) -> str:
    """The request for one automatic CI fix task on review ``number``."""

    label = f"PR #{number}" if number else "the review"
    reference = f"{label} ({url})" if url else label
    base = base_branch or "main"
    names = ", ".join(f"'{name}'" for name in check_names) or "its checks"
    evidence = []
    for failure in failures:
        ids = []
        if failure.run_id:
            ids.append(f"run {failure.run_id}")
        if failure.job_id:
            ids.append(f"job {failure.job_id}")
        detail = f", conclusion {failure.detail}" if failure.detail else ""
        evidence.append(f"'{failure.name}' ({', '.join(ids) or 'no run id'}{detail})")
    evidence_text = f" Failed jobs: {'; '.join(evidence)}." if evidence else ""
    return (
        f"Fix the failing CI on {reference}, head {head}. {goal} "
        f"Failed checks: {names}.{evidence_text} "
        "The job logs require authentication, so read them with the authenticated gh "
        "CLI, for example `gh run view <run> --log-failed` or `gh run view --job <id> "
        "--log-failed`, and identify the exact failing tests and errors for each job. "
        f"Then decide the cause. (A) If {label}'s change caused a failure, fix the root "
        f"cause on {label}'s own branch: stay on your task branch, run `git fetch origin "
        f"pull/{number}/head` and `git reset --hard {head}`, commit the fix, and push it "
        f"with `git push origin HEAD:refs/heads/<head branch>` (find the branch with "
        f"`gh pr view {number} --json headRefName`). Never force-push and do not open a "
        f"separate PR for this. (B) If a failure predates {label} and also reproduces on "
        f"{base}, fix its root cause in a separate PR based on {base} and do not push to "
        f"{label}; that fix needs its own merge. Never skip tests, mark them xfail, "
        "loosen assertions, or merge any PR. If a change touches libraries/provider-kit "
        "or provider-library functions the body uses, also bump the pinned revisions in "
        "genesis.toml, pyproject.toml and libraries/*/pyproject.toml (provider-kit "
        "first, then the reference providers); otherwise only the Full-body depth-2 "
        "descent check goes red. Tests that can requeue work must patch "
        "EnochApplication._maybe_start_task_worker. Reproduce locally where possible and "
        "run the full pytest suite after fixing. Do not re-run CI to get a green result "
        "without a fix; if the failure is clearly an infrastructure problem, say so and "
        "push nothing. End your reply with exactly one result block: "
        f"[{CI_FIX_RESULT_TAG}] followed by one JSON object and [/{CI_FIX_RESULT_TAG}]. "
        'Use {"outcome": "pushed", "previous_head_sha": "<head you reset to>", '
        '"head_sha": "<full SHA you pushed>"} for (A), {"outcome": "main-failure", '
        '"review_url": "<URL of the separate PR>"} for (B), or {"outcome": "no-change", '
        '"reason": "<why>"} when you pushed nothing.'
    )


# Fix task results ---------------------------------------------------------------


@dataclass(frozen=True)
class CiFixResult:
    outcome: str
    head_sha: str = ""
    previous_head_sha: str = ""
    review_url: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CiFixExtraction:
    visible_reply: str
    results: tuple[CiFixResult, ...]


def extract_ci_fix_results(reply: str) -> CiFixExtraction:
    """Strip ``[ENOCH_CI_FIX_RESULT]`` blocks from ``reply`` and parse them."""

    results = []
    for match in _SIGNAL.finditer(reply or ""):
        parsed = _parse_result(match.group(1))
        if parsed is not None:
            results.append(parsed)
    visible = _SIGNAL.sub("", reply or "")
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return CiFixExtraction(visible, tuple(results))


def _parse_result(raw: str) -> CiFixResult | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    outcome = str(data.get("outcome") or "").strip().lower()
    if outcome not in CI_FIX_OUTCOMES:
        return None
    head = str(data.get("head_sha") or "").strip()
    previous = str(data.get("previous_head_sha") or "").strip()
    if outcome == "pushed" and not (_REVISION.fullmatch(head) and _REVISION.fullmatch(previous)):
        return None
    review_url = str(data.get("review_url") or "").strip()
    if outcome == "main-failure" and not review_url:
        return None
    return CiFixResult(
        outcome=outcome,
        head_sha=head,
        previous_head_sha=previous,
        review_url=review_url,
        reason=str(data.get("reason") or "").strip()[:500],
    )


def ci_fix_results_path(root: Path | None = None) -> Path:
    return private_state_path(CI_FIX_RESULTS_STATE_FILE, root)


def record_ci_fix_result(task_id: int, result: CiFixResult, root: Path | None = None) -> None:
    """Store the latest result a fix task reported (later reports replace earlier ones)."""

    path = ci_fix_results_path(root)
    data = load_json_object(path)
    results = data.get("results") if isinstance(data.get("results"), dict) else {}
    results = dict(results)
    results[str(task_id)] = {
        "outcome": result.outcome,
        "head_sha": result.head_sha,
        "previous_head_sha": result.previous_head_sha,
        "review_url": result.review_url,
        "reason": result.reason,
    }
    atomic_write(
        path,
        json.dumps({"schema_version": 1, "results": results}, indent=2, sort_keys=True) + "\n",
    )


def load_ci_fix_result(task_id: int, root: Path | None = None) -> CiFixResult | None:
    data = load_json_object(ci_fix_results_path(root))
    results = data.get("results")
    if not isinstance(results, dict):
        return None
    raw = results.get(str(task_id))
    if not isinstance(raw, dict):
        return None
    return _parse_result(json.dumps(raw))
