from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlparse

from enoch.config import read_section
from enoch.git_tools import (
    GitError,
    changed_files,
    current_branch,
    diff_summary,
    ensure_clean_worktree,
    run_git,
)
from enoch.immune import ImmuneResult, run_immune_system
from enoch.runtime import DEFAULT_BRANCH, DEFAULT_REMOTE, PROTECTED_BRANCHES


DEFAULT_PROTECTED_BRANCHES = PROTECTED_BRANCHES
GITHUB_NOREPLY_DOMAIN = "users.noreply.github.com"


class PublishError(RuntimeError):
    pass


def feature_title(text: str) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:72].strip() or "Enoch feature"


@dataclass(frozen=True)
class LocalPublishResult:
    branch: str
    commit_message: str
    changed_files: list[str]
    diff: str
    doctor: ImmuneResult
    commit_sha: str


@dataclass(frozen=True)
class RemotePublishResult:
    branch: str
    remote: str
    pushed: bool
    ahead_count: int
    compare_url: str | None


@dataclass(frozen=True)
class PullRequestResult:
    branch: str
    title: str
    body: str
    created: bool
    url: str | None
    fallback_url: str | None
    note: str | None = None
    draft: bool = False


@dataclass(frozen=True)
class EvolutionProvenance:
    candidate_id: str
    evidence_source: str
    signal_actor: str
    candidate_actor: str
    approval_actor: str
    task_id: int
    parent_candidate_id: str = ""
    source_task_id: int | None = None
    retry_of_task_id: int | None = None


@dataclass(frozen=True)
class PullRequestCloseResult:
    number: int
    closed: bool
    url: str
    note: str | None = None


@dataclass(frozen=True)
class PullRequestMergeStatus:
    reference: str
    url: str
    state: str
    base_branch: str
    merge_commit: str
    merged_at: str
    note: str | None = None


@dataclass(frozen=True)
class PullRequestTarget:
    reference: str
    number: int
    repository: str = ""


@dataclass(frozen=True)
class PullRequestMergeCandidate:
    target: PullRequestTarget
    number: int
    repository: str
    url: str
    state: str
    is_draft: bool
    mergeable: str
    merge_state_status: str
    head_oid: str
    base_branch: str


@dataclass(frozen=True)
class PullRequestMergeResult:
    number: int
    url: str
    method: str
    merge_commit: str
    message: str
    merged: bool = True


_PR_NUMBER_PATTERN = re.compile(r"^[0-9]+$")


def prepare_local_publish(
    commit_message: str,
    root: Path | None = None,
    allow_protected_branch: bool = False,
    allowed_files: list[str] | tuple[str, ...] | None = None,
) -> LocalPublishResult:
    message = commit_message.strip()
    if not message:
        raise PublishError("Commit message cannot be empty.")

    branch = current_branch(root)
    if branch in DEFAULT_PROTECTED_BRANCHES and not allow_protected_branch:
        raise PublishError(
            f"Refusing to publish from {branch}. Create a feature branch or explicitly allow a protected branch."
        )

    files = changed_files(root)
    if allowed_files is not None:
        allowed = {path for path in allowed_files if path}
        unexpected = sorted(path for path in files if path not in allowed)
        if unexpected:
            details = ", ".join(unexpected[:8])
            raise PublishError(f"Refusing to publish unexpected files: {details}")
        files = [path for path in files if path in allowed]
    if not files:
        raise PublishError("No local changes to publish.")

    diff = diff_summary(root)
    doctor = run_immune_system(root)
    if not doctor.passed:
        raise PublishError(f"Doctor failed: {doctor.diagnosis.summary}")

    _git_or_raise(["add", "--", *files], root, "Could not stage local changes.")
    _git_or_raise(_commit_command(message, root), root, "Could not commit local changes.")
    commit_sha = _git_or_raise(["rev-parse", "--short", "HEAD"], root, "Could not read commit SHA.")

    return LocalPublishResult(
        branch=branch,
        commit_message=message,
        changed_files=files,
        diff=diff,
        doctor=doctor,
        commit_sha=commit_sha,
    )


def push_current_branch(
    root: Path | None = None,
    remote: str = DEFAULT_REMOTE,
    base_branch: str = DEFAULT_BRANCH,
    allow_protected_branch: bool = False,
) -> RemotePublishResult:
    branch = current_branch(root)
    if branch in DEFAULT_PROTECTED_BRANCHES and not allow_protected_branch:
        raise PublishError(
            f"Refusing to push {branch}. Create a feature branch or explicitly allow a protected branch."
        )

    ensure_clean_worktree(root)
    ahead_count = _ahead_count(remote, branch, root, base_branch=base_branch)
    if ahead_count <= 0:
        raise PublishError(f"{branch} has no local commits ahead of {remote}/{branch}.")

    _git_or_raise(["push", "-u", remote, branch], root, f"Could not push {branch}.")
    return RemotePublishResult(
        branch=branch,
        remote=remote,
        pushed=True,
        ahead_count=ahead_count,
        compare_url=_compare_url(remote, base_branch, branch, root),
    )


def create_pull_request(
    title: str | None = None,
    body: str | None = None,
    root: Path | None = None,
    remote: str = DEFAULT_REMOTE,
    base_branch: str = DEFAULT_BRANCH,
    allow_protected_branch: bool = False,
    draft: bool = False,
    evolution_provenance: EvolutionProvenance | None = None,
) -> PullRequestResult:
    branch = current_branch(root)
    if branch in DEFAULT_PROTECTED_BRANCHES and not allow_protected_branch:
        raise PublishError(
            f"Refusing to open a PR from {branch}. Create a feature branch first."
        )

    ensure_clean_worktree(root)
    _ensure_upstream(remote, branch, root)
    pr_title = title or _latest_commit_subject(root)
    pr_body = body or _default_pr_body(branch, root)
    if evolution_provenance is not None:
        pr_body = _append_evolution_provenance(pr_body, evolution_provenance)
    fallback_url = _compare_url(remote, base_branch, branch, root)
    gh = shutil.which("gh")
    if gh is None:
        return PullRequestResult(
            branch=branch,
            title=pr_title,
            body=pr_body,
            created=False,
            url=None,
            fallback_url=fallback_url,
            note="GitHub CLI is not available.",
            draft=draft,
        )

    command = [
        gh,
        "pr",
        "create",
        "--base",
        base_branch,
        "--head",
        branch,
        "--title",
        pr_title,
        "--body",
        pr_body,
    ]
    if draft:
        command.append("--draft")
    result = subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub CLI could not create the PR."
        return PullRequestResult(
            branch=branch,
            title=pr_title,
            body=pr_body,
            created=False,
            url=None,
            fallback_url=fallback_url,
            note=note,
            draft=draft,
        )

    return PullRequestResult(
        branch=branch,
        title=pr_title,
        body=pr_body,
        created=True,
        url=result.stdout.strip() or None,
        fallback_url=fallback_url,
        draft=draft,
    )


def close_pull_request(
    number: int,
    root: Path | None = None,
    comment: str | None = None,
    remote: str = DEFAULT_REMOTE,
) -> PullRequestCloseResult:
    if number <= 0:
        raise PublishError("Pull request number must be positive.")
    url = _pull_request_url(remote, number, root)
    gh = shutil.which("gh")
    if gh is None:
        return PullRequestCloseResult(
            number=number,
            closed=False,
            url=url,
            note="GitHub CLI is not available.",
        )
    command = [gh, "pr", "close", str(number)]
    if comment:
        command.extend(["--comment", comment])
    result = subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub CLI could not close the pull request."
        return PullRequestCloseResult(number=number, closed=False, url=url, note=note)
    return PullRequestCloseResult(number=number, closed=True, url=url)


def inspect_pull_request_merge(
    reference: str,
    root: Path | None = None,
) -> PullRequestMergeStatus:
    cleaned = reference.strip()
    if not cleaned:
        raise PublishError("Pull request reference is required.")
    gh = shutil.which("gh")
    if gh is None:
        raise PublishError("GitHub CLI is not available.")
    result = subprocess.run(
        [
            gh,
            "pr",
            "view",
            cleaned,
            "--json",
            "state,mergedAt,mergeCommit,baseRefName,url",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub CLI could not inspect the PR."
        raise PublishError(note)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PublishError("GitHub CLI returned invalid pull request data.") from error
    if not isinstance(data, dict):
        raise PublishError("GitHub CLI returned invalid pull request data.")
    merge_commit = data.get("mergeCommit")
    merge_oid = (
        str(merge_commit.get("oid") or "").strip()
        if isinstance(merge_commit, dict)
        else ""
    )
    return PullRequestMergeStatus(
        reference=cleaned,
        url=str(data.get("url") or cleaned).strip(),
        state=str(data.get("state") or "").strip().upper(),
        base_branch=str(data.get("baseRefName") or "").strip(),
        merge_commit=merge_oid,
        merged_at=str(data.get("mergedAt") or "").strip(),
    )


def parse_pull_request_target(reference: str) -> PullRequestTarget:
    cleaned = reference.strip()
    if not cleaned:
        raise PublishError("Pull request target is required.")
    if _PR_NUMBER_PATTERN.fullmatch(cleaned):
        number = int(cleaned)
        if number <= 0:
            raise PublishError("Pull request number must be positive.")
        return PullRequestTarget(reference=str(number), number=number)

    parsed = urlparse(cleaned)
    path_parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 4
        or path_parts[2] != "pull"
        or not _PR_NUMBER_PATTERN.fullmatch(path_parts[3])
    ):
        raise PublishError(
            "Use a positive PR number or a full URL like "
            "https://github.com/owner/repository/pull/123."
        )
    owner, repository = path_parts[0], path_parts[1]
    number = int(path_parts[3])
    if not owner or not repository or number <= 0:
        raise PublishError(
            "Use a positive PR number or a full URL like "
            "https://github.com/owner/repository/pull/123."
        )
    repo = f"{owner}/{repository}"
    return PullRequestTarget(
        reference=f"https://github.com/{repo}/pull/{number}",
        number=number,
        repository=repo,
    )


def inspect_pull_request_candidate(
    reference: str,
    root: Path | None = None,
) -> PullRequestMergeCandidate:
    target = parse_pull_request_target(reference)
    gh = shutil.which("gh")
    if gh is None:
        raise PublishError("GitHub CLI is not available.")
    result = subprocess.run(
        [
            gh,
            "pr",
            "view",
            target.reference,
            "--json",
            (
                "number,url,state,isDraft,mergeable,mergeStateStatus,"
                "headRefOid,baseRefName,mergedAt"
            ),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub could not find that PR."
        raise PublishError(f"Could not inspect {target.reference}: {note}")
    data = _github_json_object(result.stdout, "pull request")
    try:
        number = int(data.get("number"))
    except (TypeError, ValueError) as error:
        raise PublishError("GitHub CLI returned invalid pull request data.") from error
    url = str(data.get("url") or "").strip()
    canonical = parse_pull_request_target(url)
    if number != target.number or canonical.number != target.number:
        raise PublishError("GitHub returned a different pull request than the requested target.")
    if target.repository and canonical.repository.lower() != target.repository.lower():
        raise PublishError("GitHub returned a different pull request than the requested target.")

    candidate = PullRequestMergeCandidate(
        target=target,
        number=number,
        repository=canonical.repository,
        url=canonical.reference,
        state=str(data.get("state") or "").strip().upper(),
        is_draft=bool(data.get("isDraft")),
        mergeable=str(data.get("mergeable") or "").strip().upper(),
        merge_state_status=str(data.get("mergeStateStatus") or "").strip().upper(),
        head_oid=str(data.get("headRefOid") or "").strip(),
        base_branch=str(data.get("baseRefName") or "").strip(),
    )
    _ensure_pull_request_mergeable(candidate, merged_at=str(data.get("mergedAt") or "").strip())
    return candidate


def merge_pull_request(
    reference: str,
    root: Path | None = None,
) -> PullRequestMergeResult:
    candidate = inspect_pull_request_candidate(reference, root)
    gh = shutil.which("gh")
    if gh is None:
        raise PublishError("GitHub CLI is not available.")
    method = _repository_merge_method(candidate.repository, gh, root)
    result = subprocess.run(
        [
            gh,
            "api",
            "--method",
            "PUT",
            f"repos/{candidate.repository}/pulls/{candidate.number}/merge",
            "-f",
            f"sha={candidate.head_oid}",
            "-f",
            f"merge_method={method}",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub could not merge the PR."
        raise PublishError(f"GitHub could not merge PR #{candidate.number}: {note}")
    data = _github_json_object(result.stdout, "merge result")
    message = str(data.get("message") or "").strip()
    if data.get("merged") is not True:
        raise PublishError(message or f"GitHub did not merge PR #{candidate.number}.")
    return PullRequestMergeResult(
        number=candidate.number,
        url=candidate.url,
        method=method,
        merge_commit=str(data.get("sha") or "").strip(),
        message=message or "Pull request merged.",
    )


def _ensure_pull_request_mergeable(
    candidate: PullRequestMergeCandidate,
    *,
    merged_at: str,
) -> None:
    label = f"PR #{candidate.number} ({candidate.url})"
    if merged_at or candidate.state == "MERGED":
        raise PublishError(f"{label} is already merged.")
    if candidate.state == "CLOSED":
        raise PublishError(f"{label} is closed.")
    if candidate.state != "OPEN":
        state = candidate.state or "unknown"
        raise PublishError(f"{label} is not open (GitHub reports {state}).")
    if candidate.is_draft:
        raise PublishError(f"{label} is a draft. Mark it ready for review before merging it.")
    if candidate.mergeable == "CONFLICTING":
        raise PublishError(f"{label} has merge conflicts.")
    if candidate.mergeable != "MERGEABLE":
        status = candidate.mergeable or "UNKNOWN"
        raise PublishError(f"{label} is not currently mergeable (GitHub reports {status}).")
    if candidate.merge_state_status != "CLEAN":
        status = candidate.merge_state_status or "UNKNOWN"
        raise PublishError(
            f"{label} is not ready to merge (GitHub merge state: {status})."
        )
    if not candidate.head_oid:
        raise PublishError(f"{label} has no inspectable head commit.")


def _repository_merge_method(repository: str, gh: str, root: Path | None) -> str:
    result = subprocess.run(
        [
            gh,
            "repo",
            "view",
            repository,
            "--json",
            (
                "nameWithOwner,viewerDefaultMergeMethod,mergeCommitAllowed,"
                "squashMergeAllowed,rebaseMergeAllowed"
            ),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub could not inspect merge methods."
        raise PublishError(f"Could not inspect merge methods for {repository}: {note}")
    data = _github_json_object(result.stdout, "repository")
    returned_repo = str(data.get("nameWithOwner") or "").strip()
    if returned_repo.lower() != repository.lower():
        raise PublishError("GitHub returned a different repository than the requested PR target.")
    allowed = {
        "MERGE": data.get("mergeCommitAllowed") is True,
        "SQUASH": data.get("squashMergeAllowed") is True,
        "REBASE": data.get("rebaseMergeAllowed") is True,
    }
    default = str(data.get("viewerDefaultMergeMethod") or "").strip().upper()
    if allowed.get(default):
        return default.lower()
    for method in ("MERGE", "SQUASH", "REBASE"):
        if allowed[method]:
            return method.lower()
    raise PublishError(f"{repository} does not allow a supported pull request merge method.")


def _github_json_object(output: str, label: str) -> dict[str, object]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise PublishError(f"GitHub CLI returned invalid {label} data.") from error
    if not isinstance(data, dict):
        raise PublishError(f"GitHub CLI returned invalid {label} data.")
    return data


def _git_or_raise(args: list[str], root: Path | None, fallback: str) -> str:
    result = run_git(args, root)
    if result.returncode != 0:
        raise GitError(result.stderr or fallback)
    return result.stdout


def _commit_command(message: str, root: Path | None) -> list[str]:
    author = _publish_author(root)
    command = ["commit", "-m", message]
    if author.name:
        command = ["-c", f"user.name={author.name}", *command]
    if author.email:
        command = ["-c", f"user.email={author.email}", *command]
    return command


@dataclass(frozen=True)
class _PublishAuthor:
    name: str
    email: str


def _publish_author(root: Path | None) -> _PublishAuthor:
    git_config = read_section("git", root)
    name = (
        os.environ.get("ENOCH_GIT_AUTHOR_NAME")
        or git_config.get("author_name")
        or _git_config_value("user.name", root)
        or _latest_author_value("%an", root)
    )
    email = (
        os.environ.get("ENOCH_GIT_AUTHOR_EMAIL")
        or git_config.get("author_email")
        or _noreply_latest_author_email(root)
        or _git_config_value("user.email", root)
    )
    return _PublishAuthor(name=name.strip(), email=email.strip())


def _noreply_latest_author_email(root: Path | None) -> str:
    email = _latest_author_value("%ae", root)
    if GITHUB_NOREPLY_DOMAIN in email:
        return email
    return ""


def _latest_author_value(format_: str, root: Path | None) -> str:
    result = run_git(["log", "-1", f"--pretty={format_}"], root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_config_value(key: str, root: Path | None) -> str:
    result = run_git(["config", "--get", key], root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _ahead_count(remote: str, branch: str, root: Path | None, base_branch: str = DEFAULT_BRANCH) -> int:
    result = run_git(["rev-list", "--count", f"{remote}/{branch}..HEAD"], root)
    if result.returncode != 0:
        result = run_git(["rev-list", "--count", f"{remote}/{base_branch}..HEAD"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect local commits.")
    try:
        return int(result.stdout.strip())
    except ValueError as error:
        raise GitError("Could not parse local commit count.") from error


def _ensure_upstream(remote: str, branch: str, root: Path | None) -> None:
    result = run_git(["rev-parse", "--verify", f"{remote}/{branch}"], root)
    if result.returncode != 0:
        raise PublishError(f"{branch} has not been pushed to {remote}. Push this branch first.")


def _latest_commit_subject(root: Path | None) -> str:
    result = run_git(["log", "-1", "--pretty=%s"], root)
    if result.returncode != 0 or not result.stdout.strip():
        return "Enoch evolution"
    return result.stdout.strip()


def _default_pr_body(branch: str, root: Path | None) -> str:
    commit = _latest_commit_subject(root)
    return "\n".join(
        [
            "## Summary",
            f"- Prepared Enoch evolution from `{branch}`.",
            f"- Latest commit: {commit}",
            "",
            "## Validation",
            "- Run `bin/enoch` and `doctor` locally as needed before review.",
            "",
            "## Human Review",
            "- PR created for review.",
        ]
    )


def format_evolution_provenance(provenance: EvolutionProvenance) -> str:
    lines = [
        "## Evolution provenance",
        "",
        f"- Candidate: `{provenance.candidate_id}`",
        f"- Evidence source: {provenance.evidence_source}",
        f"- Signal actor: {provenance.signal_actor}",
        f"- Candidate actor: {provenance.candidate_actor}",
        f"- Approval actor: {provenance.approval_actor}",
        f"- Task: #{provenance.task_id}",
    ]
    if provenance.parent_candidate_id:
        lines.append(f"- Parent candidate: `{provenance.parent_candidate_id}`")
    if provenance.source_task_id is not None:
        lines.append(f"- Source task: #{provenance.source_task_id}")
    if provenance.retry_of_task_id is not None:
        lines.append(f"- Retry of task: #{provenance.retry_of_task_id}")
    return "\n".join(lines)


def _append_evolution_provenance(body: str, provenance: EvolutionProvenance) -> str:
    if "## Evolution provenance" in body:
        return body
    return "\n\n".join([body.rstrip(), format_evolution_provenance(provenance)])


def _compare_url(remote: str, base_branch: str, branch: str, root: Path | None) -> str | None:
    result = run_git(["remote", "get-url", remote], root)
    if result.returncode != 0:
        return None
    repo = _github_repo_from_remote(result.stdout)
    if repo is None:
        return None
    return f"https://github.com/{repo}/compare/{base_branch}...{branch}?expand=1"


def _pull_request_url(remote: str, number: int, root: Path | None) -> str:
    result = run_git(["remote", "get-url", remote], root)
    if result.returncode != 0:
        return ""
    repo = _github_repo_from_remote(result.stdout)
    if repo is None:
        return ""
    return f"https://github.com/{repo}/pull/{number}"


def _github_repo_from_remote(remote_url: str) -> str | None:
    value = remote_url.strip()
    repo = ""
    if value.startswith("git@github.com:"):
        repo = value.split(":", 1)[1]
    elif value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.netloc.lower() != "github.com":
            return None
        repo = parsed.path.lstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    owner, separator, name = repo.strip("/").partition("/")
    if not separator or not owner or not name:
        return None
    return f"{owner}/{name}"
