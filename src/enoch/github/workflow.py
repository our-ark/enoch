from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
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
    source: str
    task_id: int
    retry_of_task_id: int | None = None


@dataclass(frozen=True)
class PullRequestCloseResult:
    number: int
    closed: bool
    url: str
    note: str | None = None


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
        f"- Source: {provenance.source}",
        f"- Task: #{provenance.task_id}",
    ]
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
