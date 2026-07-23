from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlparse

from our_ark_provider_kit import (
    agent_context,
    EvolutionProvenance,
    ForgeProviderError,
    LocalPublishResult,
    PullRequestCloseResult,
    PullRequestMergeCandidate,
    PullRequestMergeResult,
    PullRequestMergeStatus,
    PullRequestResult,
    PullRequestTarget,
    RemotePublishResult,
)


DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
DEFAULT_PROTECTED_BRANCHES = {DEFAULT_BRANCH, "master"}
GITHUB_NOREPLY_DOMAIN = "users.noreply.github.com"
_POSITIVE_PR_NUMBER = re.compile(r"^[1-9][0-9]*$")
_GITHUB_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class PublishError(ForgeProviderError):
    pass


GitError = PublishError


def _agent_module(component: str, root: Path | None = None):
    return agent_context(root).module(component)


def read_section(section: str, root: Path | None = None):
    return _agent_module("config", root).read_section(section, root)


def changed_files(root: Path | None = None):
    return _agent_module("git_tools", root).changed_files(root)


def current_branch(root: Path | None = None):
    return _agent_module("git_tools", root).current_branch(root)


def diff_summary(root: Path | None = None):
    return _agent_module("git_tools", root).diff_summary(root)


def ensure_clean_worktree(root: Path | None = None):
    return _agent_module("git_tools", root).ensure_clean_worktree(root)


def run_git(args: list[str], root: Path | None = None):
    return _agent_module("git_tools", root).run_git(args, root)


def run_immune_system(root: Path | None = None):
    return _agent_module("immune", root).run_immune_system(root)


def feature_title(text: str) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:72].strip() or "Agent feature"


_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefOid,headRefName,baseRefName,author,updatedAt,mergedAt"
)
_PR_LIST_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefOid,headRefName,baseRefName,author,updatedAt"
)


def prepare_local_publish(
    commit_message: str,
    root: Path | None = None,
    allow_protected_branch: bool = False,
    allowed_files: list[str] | tuple[str, ...] | None = None,
    validation_result: object | None = None,
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
    doctor = validation_result or run_immune_system(root)
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
        if "already exists" in note.lower():
            existing = subprocess.run(
                [
                    gh,
                    "pr",
                    "view",
                    branch,
                    "--json",
                    "url,title,body,isDraft,state",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            if existing.returncode == 0:
                try:
                    existing_data = json.loads(existing.stdout)
                except json.JSONDecodeError:
                    existing_data = {}
                existing_url = str(existing_data.get("url") or "").strip()
                if existing_url and str(existing_data.get("state") or "OPEN").upper() == "OPEN":
                    return PullRequestResult(
                        branch=branch,
                        title=str(existing_data.get("title") or pr_title),
                        body=str(existing_data.get("body") or pr_body),
                        created=True,
                        url=existing_url,
                        fallback_url=fallback_url,
                        note="Reused the existing open pull request for this branch.",
                        draft=bool(existing_data.get("isDraft", draft)),
                    )
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
                "number,state,isDraft,mergeable,mergeStateStatus,mergedAt,"
                "mergeCommit,baseRefName,headRefOid,url"
            ),
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
    url = str(data.get("url") or "").strip()
    try:
        resolved_target = parse_pull_request_target(url)
    except PublishError as error:
        raise PublishError("GitHub CLI returned an invalid pull request URL.") from error
    returned_number = data.get("number")
    if isinstance(returned_number, int) and returned_number != resolved_target.number:
        raise PublishError("GitHub CLI returned inconsistent pull request data.")
    if target.repository and (
        resolved_target.repository.casefold() != target.repository.casefold()
        or resolved_target.number != target.number
    ):
        raise PublishError("GitHub returned a different pull request than the requested URL.")
    return PullRequestMergeStatus(
        reference=target.reference,
        url=resolved_target.reference,
        state=str(data.get("state") or "").strip().upper(),
        base_branch=str(data.get("baseRefName") or "").strip(),
        merge_commit=merge_oid,
        merged_at=str(data.get("mergedAt") or "").strip(),
        number=resolved_target.number,
        repository=resolved_target.repository,
        is_draft=bool(data.get("isDraft")),
        mergeable=str(data.get("mergeable") or "").strip().upper(),
        merge_state_status=str(data.get("mergeStateStatus") or "").strip().upper(),
        head_sha=str(data.get("headRefOid") or "").strip(),
    )


def parse_pull_request_target(reference: str) -> PullRequestTarget:
    cleaned = reference.strip()
    if _POSITIVE_PR_NUMBER.fullmatch(cleaned):
        return PullRequestTarget(reference=str(int(cleaned)), number=int(cleaned))

    parsed = urlparse(cleaned)
    parts = [part for part in parsed.path.split("/") if part]
    valid_url = (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower() == "github.com"
        and not parsed.params
        and len(parts) == 4
        and parts[2] == "pull"
        and _GITHUB_REPOSITORY_PART.fullmatch(parts[0]) is not None
        and _GITHUB_REPOSITORY_PART.fullmatch(parts[1]) is not None
        and _POSITIVE_PR_NUMBER.fullmatch(parts[3]) is not None
    )
    if not valid_url:
        raise PublishError(
            "Use a positive PR number or a full "
            "https://github.com/OWNER/REPO/pull/NUMBER URL."
        )
    number = int(parts[3])
    repository = f"{parts[0]}/{parts[1]}"
    return PullRequestTarget(
        reference=f"https://github.com/{repository}/pull/{number}",
        number=number,
        repository=repository,
    )


def list_open_pull_requests(
    root: Path | None = None,
    *,
    limit: int = 20,
) -> tuple[PullRequestMergeCandidate, ...]:
    gh = shutil.which("gh")
    if gh is None:
        raise PublishError("GitHub CLI is not available.")
    result = subprocess.run(
        [
            gh,
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(max(1, min(limit, 50))),
            "--json",
            _PR_LIST_FIELDS,
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = result.stderr.strip() or result.stdout.strip() or "GitHub could not list pull requests."
        raise PublishError(f"Could not list open pull requests: {note}")
    data = _github_json_array(result.stdout, "pull request list")
    pull_requests = []
    for item in data:
        if not isinstance(item, dict):
            raise PublishError("GitHub CLI returned invalid pull request list data.")
        url = str(item.get("url") or "").strip()
        target = parse_pull_request_target(url)
        pull_request = _pull_request_candidate(target, item)
        if pull_request.state == "OPEN":
            pull_requests.append(pull_request)
    return tuple(pull_requests)


def inspect_pull_request(
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
            _PR_VIEW_FIELDS,
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
    candidate = _pull_request_candidate(target, data)
    if candidate.number != target.number:
        raise PublishError("GitHub returned a different pull request than the requested target.")
    if target.repository and candidate.repository.lower() != target.repository.lower():
        raise PublishError("GitHub returned a different pull request than the requested target.")
    return candidate


def merge_pull_request(
    reference: str,
    root: Path | None = None,
) -> PullRequestMergeResult:
    status = inspect_pull_request_merge(reference, root)
    _require_mergeable_pull_request(status)
    gh = shutil.which("gh")
    if gh is None:
        raise PublishError("GitHub CLI is not available.")
    method = _repository_merge_method(gh, status.repository, root)
    payload = json.dumps({"sha": status.head_sha, "merge_method": method})
    result = subprocess.run(
        [
            gh,
            "api",
            "--method",
            "PUT",
            f"repos/{status.repository}/pulls/{status.number}/merge",
            "--input",
            "-",
        ],
        cwd=root,
        text=True,
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = (
            result.stderr.strip()
            or result.stdout.strip()
            or "GitHub could not merge the pull request."
        )
        raise PublishError(note)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PublishError("GitHub returned invalid merge data.") from error
    if not isinstance(data, dict):
        raise PublishError("GitHub returned invalid merge data.")
    message = str(data.get("message") or "").strip()
    if data.get("merged") is not True:
        raise PublishError(message or "GitHub did not merge the pull request.")
    return PullRequestMergeResult(
        number=status.number,
        url=status.url,
        method=method,
        merge_commit=str(data.get("sha") or "").strip(),
        message=message or "Pull request merged.",
    )


def _require_mergeable_pull_request(status: PullRequestMergeStatus) -> None:
    label = f"PR #{status.number}" if status.number else "Pull request"
    if status.state == "MERGED" or status.merged_at:
        raise PublishError(f"{label} is already merged.")
    if status.state == "CLOSED":
        raise PublishError(f"{label} is closed.")
    if status.state != "OPEN":
        raise PublishError(f"{label} is not open (state: {status.state or 'unknown'}).")
    if status.is_draft:
        raise PublishError(f"{label} is a draft. Mark it ready explicitly before merging.")
    if status.mergeable == "CONFLICTING" or status.merge_state_status == "DIRTY":
        raise PublishError(f"{label} has merge conflicts.")
    if status.mergeable != "MERGEABLE":
        raise PublishError(f"{label} is not currently mergeable (status: {status.mergeable or 'unknown'}).")
    if status.merge_state_status not in {"CLEAN", "UNSTABLE"}:
        raise PublishError(
            f"{label} is not currently mergeable "
            f"(merge state: {status.merge_state_status.lower() or 'unknown'})."
        )
    if not status.head_sha:
        raise PublishError(f"{label} has no inspectable head commit.")


def _repository_merge_method(gh: str, repository: str, root: Path | None) -> str:
    result = subprocess.run(
        [
            gh,
            "repo",
            "view",
            repository,
            "--json",
            "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        note = (
            result.stderr.strip()
            or result.stdout.strip()
            or "GitHub could not inspect merge methods."
        )
        raise PublishError(note)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PublishError("GitHub returned invalid repository merge settings.") from error
    if not isinstance(data, dict):
        raise PublishError("GitHub returned invalid repository merge settings.")
    for method, setting in (
        ("merge", "mergeCommitAllowed"),
        ("squash", "squashMergeAllowed"),
        ("rebase", "rebaseMergeAllowed"),
    ):
        if data.get(setting) is True:
            return method
    raise PublishError("The repository has no supported pull request merge method enabled.")


def _github_json_object(output: str, label: str) -> dict[str, object]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise PublishError(f"GitHub CLI returned invalid {label} data.") from error
    if not isinstance(data, dict):
        raise PublishError(f"GitHub CLI returned invalid {label} data.")
    return data


def _github_json_array(output: str, label: str) -> list[object]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise PublishError(f"GitHub CLI returned invalid {label} data.") from error
    if not isinstance(data, list):
        raise PublishError(f"GitHub CLI returned invalid {label} data.")
    return data


def _pull_request_candidate(
    target: PullRequestTarget,
    data: dict[str, object],
) -> PullRequestMergeCandidate:
    try:
        number = int(data.get("number"))
    except (TypeError, ValueError) as error:
        raise PublishError("GitHub CLI returned invalid pull request data.") from error
    canonical = parse_pull_request_target(str(data.get("url") or "").strip())
    if canonical.number != number:
        raise PublishError("GitHub returned inconsistent pull request data.")
    author_data = data.get("author")
    author = (
        str(author_data.get("login") or "").strip()
        if isinstance(author_data, dict)
        else ""
    )
    return PullRequestMergeCandidate(
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
        title=str(data.get("title") or "").strip(),
        head_branch=str(data.get("headRefName") or "").strip(),
        author=author,
        updated_at=str(data.get("updatedAt") or "").strip(),
        merged_at=str(data.get("mergedAt") or "").strip(),
    )


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
    context = agent_context(root)
    git_config = read_section("git", root)
    name = (
        os.environ.get(f"{context.env_prefix}_GIT_AUTHOR_NAME")
        or os.environ.get("OUR_ARK_GIT_AUTHOR_NAME")
        or git_config.get("author_name")
        or _git_config_value("user.name", root)
        or _latest_author_value("%an", root)
    )
    email = (
        os.environ.get(f"{context.env_prefix}_GIT_AUTHOR_EMAIL")
        or os.environ.get("OUR_ARK_GIT_AUTHOR_EMAIL")
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
        return "Agent evolution"
    return result.stdout.strip()


def _default_pr_body(branch: str, root: Path | None) -> str:
    context = agent_context(root)
    commit = _latest_commit_subject(root)
    return "\n".join(
        [
            "## Summary",
            f"- Prepared {context.name} evolution from `{branch}`.",
            f"- Latest commit: {commit}",
            "",
            "## Validation",
            f"- Run `bin/{context.service_slug}` and `doctor` locally as needed before review.",
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
