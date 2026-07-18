from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any
from urllib.parse import urlparse

from enoch.lineage.config import LineageSettings, lineage_settings
from enoch.runtime import DEFAULT_BRANCH


LINEAGE_PATH = Path(".agent") / "lineage.yaml"
LINEAGE_INBOX_PATH = Path(".agent") / "lineage_inbox.json"
CURRENT_IDENTITY_PATH = Path("src") / "enoch" / "identity.yaml"
INBOX_SCHEMA_VERSION = 1
REFRESH_LIMIT = 20
ROOT_ANCESTOR_NAMES = {"lucy"}
ROOT_ANCESTOR_REPOS = {"our-ark/lucy"}
INHERITABLE_RELEVANCE = {"high", "medium"}

STATUS_PENDING = "pending"
STATUS_IGNORED = "ignored"
STATUS_ADOPTED = "adopted"
INBOX_STATUSES = {STATUS_PENDING, STATUS_IGNORED, STATUS_ADOPTED}


class LineageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParentLink:
    name: str
    repo: str
    branch: str = DEFAULT_BRANCH
    commit_at_birth: str = ""


@dataclass(frozen=True)
class AncestorLink:
    name: str
    repo: str
    branch: str
    depth: int
    skills: tuple[str, ...] = ()
    commit_at_birth: str = ""


@dataclass(frozen=True)
class CurrentAgentProfile:
    name: str
    identity_path: Path
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class LineageCandidate:
    id: str
    repo: str
    pr_number: int
    title: str
    url: str
    merged_at: str
    merge_commit: str
    ancestor_name: str
    depth: int
    labels: tuple[str, ...]
    files: tuple[str, ...]
    relevance: str
    confidence: str
    reason: str
    body_excerpt: str
    status: str = STATUS_PENDING
    first_seen_at: str = ""
    last_seen_at: str = ""
    reviewed_at: str = ""
    review_note: str = ""


@dataclass(frozen=True)
class LineageInboxReport:
    scope: str
    ancestors: tuple[AncestorLink, ...]
    candidates: tuple[LineageCandidate, ...]
    latest_heads: dict[str, str]
    errors: tuple[str, ...] = ()
    refreshed_at: str = ""
    new_count: int = 0


@dataclass(frozen=True)
class LineageResolution:
    ancestors: tuple[AncestorLink, ...]
    warnings: tuple[str, ...] = ()


class LineageGitHubClient:
    def __init__(self, gh: str | None = None) -> None:
        self.gh = gh or shutil.which("gh")
        if self.gh is None:
            raise LineageError("GitHub CLI is not available.")

    def remote_parent(self, repo: str, branch: str) -> ParentLink | None:
        data = self._json(["api", f"repos/{repo}/contents/{LINEAGE_PATH.as_posix()}?ref={branch}"])
        content = str(data.get("content") or "")
        if not content:
            return None
        decoded = base64.b64decode(content).decode("utf-8")
        return parse_lineage_parent(decoded)

    def latest_commit(self, repo: str, branch: str) -> str:
        data = self._json(["api", f"repos/{repo}/commits/{branch}"])
        sha = str(data.get("sha") or "").strip()
        if not sha:
            raise LineageError(f"Could not read latest commit for {repo}:{branch}.")
        return sha

    def declared_skills(self, repo: str, branch: str) -> tuple[str, ...]:
        data = self._json(["api", f"repos/{repo}/contents/src/{repo.split('/')[-1]}/identity.yaml?ref={branch}"])
        content = str(data.get("content") or "")
        if not content:
            return ()
        decoded = base64.b64decode(content).decode("utf-8")
        return parse_declared_skills(decoded)

    def merged_prs(self, repo: str, branch: str, limit: int = REFRESH_LIMIT) -> list[dict[str, Any]]:
        return list(
            self._json(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "merged",
                    "--base",
                    branch,
                    "--limit",
                    str(limit),
                    "--json",
                    "number,title,body,labels,mergedAt,mergeCommit,url",
                ]
            )
        )

    def commits(self, repo: str, branch: str, limit: int = REFRESH_LIMIT) -> list[dict[str, Any]]:
        return list(
            self._json(
                [
                    "api",
                    f"repos/{repo}/commits?sha={branch}&per_page={limit}",
                ]
            )
        )

    def commit_files(self, repo: str, sha: str) -> tuple[str, ...]:
        data = self._json(["api", f"repos/{repo}/commits/{sha}"])
        return tuple(str(item.get("filename") or "") for item in data.get("files", []) if item.get("filename"))

    def pr_files(self, repo: str, number: int) -> tuple[str, ...]:
        data = self._json(["pr", "view", str(number), "--repo", repo, "--json", "files"])
        return tuple(str(item.get("path") or "") for item in data.get("files", []) if item.get("path"))

    def _json(self, args: list[str]) -> Any:
        result = subprocess.run(
            [self.gh, *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "GitHub CLI command failed."
            raise LineageError(detail)
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise LineageError("GitHub CLI returned invalid JSON.") from error


def lineage_file(root: Path | None = None) -> Path:
    return Path(root or Path.cwd()) / LINEAGE_PATH


def lineage_inbox_file(root: Path | None = None) -> Path:
    return Path(root or Path.cwd()) / LINEAGE_INBOX_PATH


def load_parent(root: Path | None = None) -> ParentLink | None:
    path = lineage_file(root)
    try:
        return parse_lineage_parent(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def load_birth_commit(root: Path | None = None) -> str:
    path = lineage_file(root)
    try:
        return parse_lineage_birth_commit(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def load_current_agent_profile(root: Path | None = None) -> CurrentAgentProfile | None:
    path = Path(root or Path.cwd()) / CURRENT_IDENTITY_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    name = parse_identity_name(text) or "Enoch"
    return CurrentAgentProfile(name=name, identity_path=CURRENT_IDENTITY_PATH, skills=parse_declared_skills(text))


def parse_lineage_parent(text: str) -> ParentLink | None:
    parent = _parse_lineage_section(text, "parent")
    name = parent.get("name", "").strip()
    repo = parent.get("repo", "").strip()
    if not name or not repo:
        return None
    return ParentLink(
        name=name,
        repo=_normalize_repo(repo),
        branch=parent.get("branch", DEFAULT_BRANCH).strip() or DEFAULT_BRANCH,
        commit_at_birth=_commit_identifier(parent.get("commit_at_birth", "")),
    )


def parse_lineage_birth_commit(text: str) -> str:
    descendant = _parse_lineage_section(text, "descendant")
    return _commit_identifier(descendant.get("birth_commit", ""))


def _parse_lineage_section(text: str, section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if not line.startswith((" ", "\t")):
            if stripped in {f"{section}: null", f"{section}: none"}:
                return {}
            in_section = stripped == f"{section}:"
            continue
        if not in_section or ":" not in line:
            continue
        key, _separator, value = line.partition(":")
        values[key.strip()] = _clean_yaml_value(value)
    return values


def _commit_identifier(value: str) -> str:
    candidate = value.strip().lower()
    if len(candidate) not in {40, 64}:
        return ""
    if any(character not in "0123456789abcdef" for character in candidate):
        return ""
    return candidate


def resolve_lineage(
    root: Path | None = None,
    client: LineageGitHubClient | None = None,
    max_depth: int = 10,
) -> LineageResolution:
    parent = load_parent(root)
    if parent is None:
        return LineageResolution(ancestors=())
    github = client or LineageGitHubClient()
    chain: list[AncestorLink] = []
    warnings: list[str] = []
    seen_repos: set[str] = set()
    current = parent
    depth = 1
    while current is not None and depth <= max_depth:
        if current.repo in seen_repos:
            raise LineageError(f"Lineage cycle detected at {current.repo}.")
        seen_repos.add(current.repo)
        branch = current.branch or DEFAULT_BRANCH
        try:
            skills = github.declared_skills(current.repo, branch)
        except LineageError:
            skills = ()
        chain.append(
            AncestorLink(
                name=current.name,
                repo=current.repo,
                branch=branch,
                depth=depth,
                skills=skills,
                commit_at_birth=current.commit_at_birth,
            )
        )
        if _is_root_ancestor(current):
            break
        lineage_ref = current.commit_at_birth or current.branch or DEFAULT_BRANCH
        try:
            current = github.remote_parent(current.repo, lineage_ref)
        except LineageError as error:
            warnings.append(f"Could not read parent lineage from {current.repo}@{lineage_ref}: {error}")
            current = None
        depth += 1
    return LineageResolution(ancestors=tuple(chain), warnings=tuple(warnings))


def _is_root_ancestor(parent: ParentLink) -> bool:
    return _is_root_ancestor_identity(parent.name, parent.repo)


def _is_root_ancestor_link(ancestor: AncestorLink) -> bool:
    return _is_root_ancestor_identity(ancestor.name, ancestor.repo)


def _is_root_ancestor_identity(name: str, repo: str) -> bool:
    return name.strip().lower() in ROOT_ANCESTOR_NAMES or repo.strip().lower() in ROOT_ANCESTOR_REPOS


def parse_declared_skills(text: str) -> tuple[str, ...]:
    skills: list[str] = []
    in_skills = False
    current_skill: dict[str, str] | None = None

    def finish_skill() -> None:
        nonlocal current_skill
        if current_skill is None:
            return
        name = current_skill.get("name", "").strip()
        exposure = current_skill.get("exposure", "").strip().lower()
        if name and exposure != "hidden":
            skills.append(name)
        current_skill = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith((" ", "\t")):
            finish_skill()
            in_skills = stripped == "skills:"
            continue
        if not in_skills:
            continue
        if stripped.startswith("- name:"):
            finish_skill()
            name = _clean_yaml_value(stripped.split(":", 1)[1]).strip()
            if name:
                current_skill = {"name": name}
            continue
        if current_skill is not None and ":" in stripped:
            key, _separator, value = stripped.partition(":")
            current_skill[key.strip()] = _clean_yaml_value(value).strip()
    finish_skill()
    return tuple(skills)


def parse_identity_name(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        key, _separator, value = line.partition(":")
        if key.strip() == "name":
            return _clean_yaml_value(value).strip()
    return ""


def refresh_lineage_inbox(
    root: Path | None = None,
    scope: str = "all",
    client: LineageGitHubClient | None = None,
) -> LineageInboxReport:
    scope = (scope or "all").strip().lower()
    if scope not in {"all", "parent"}:
        raise LineageError("Use lineage refresh or lineage refresh parent.")

    resolution = resolve_lineage(root, client=client)
    ancestors = resolution.ancestors if scope == "all" else resolution.ancestors[:1]
    refreshed_at = _now()
    existing_payload = _load_inbox_payload(root)
    existing = {
        candidate.id: candidate
        for candidate in _candidates_from_payload(existing_payload, include_inactive=True)
    }
    merged: dict[str, LineageCandidate] = dict(existing)
    latest_heads: dict[str, str] = {}
    errors: list[str] = list(resolution.warnings)
    new_count = 0

    if ancestors:
        github = client or LineageGitHubClient()
        settings = lineage_settings(root)
        for ancestor in ancestors:
            try:
                latest_heads[ancestor.repo] = github.latest_commit(ancestor.repo, ancestor.branch)
                pr_merge_commits: set[str] = set()
                for pr in github.merged_prs(ancestor.repo, ancestor.branch):
                    number = int(pr.get("number") or 0)
                    files = github.pr_files(ancestor.repo, number)
                    candidate = _candidate_from_pr(ancestor, pr, files, settings)
                    if candidate.merge_commit:
                        pr_merge_commits.add(candidate.merge_commit)
                    previous = existing.get(candidate.id)
                    if previous is None:
                        new_count += 1
                    merged[candidate.id] = _merge_candidate_metadata(candidate, previous, refreshed_at)
                for commit in github.commits(ancestor.repo, ancestor.branch):
                    sha = _commit_sha(commit)
                    if not sha or sha in pr_merge_commits:
                        continue
                    files = github.commit_files(ancestor.repo, sha)
                    candidate = _candidate_from_commit(ancestor, commit, files, settings)
                    previous = existing.get(candidate.id)
                    if previous is None:
                        new_count += 1
                    merged[candidate.id] = _merge_candidate_metadata(candidate, previous, refreshed_at)
            except (LineageError, ValueError) as error:
                errors.append(f"{ancestor.repo}: {error}")

    saved_candidates = tuple(sorted(merged.values(), key=_candidate_sort_key))
    _save_inbox_payload(
        {
            "schema_version": INBOX_SCHEMA_VERSION,
            "refreshed_at": refreshed_at,
            "scope": scope,
            "ancestors": [asdict(item) for item in ancestors],
            "latest_heads": latest_heads or dict(existing_payload.get("latest_heads") or {}),
            "errors": errors,
            "candidates": [_candidate_to_json(item) for item in saved_candidates],
        },
        root,
    )
    ancestor_repos = {ancestor.repo for ancestor in ancestors}
    pending = tuple(
        candidate
        for candidate in saved_candidates
        if candidate.status == STATUS_PENDING and (not ancestor_repos or candidate.repo in ancestor_repos)
    )
    return LineageInboxReport(
        scope=scope,
        ancestors=ancestors,
        candidates=pending,
        latest_heads=latest_heads,
        errors=tuple(errors),
        refreshed_at=refreshed_at,
        new_count=new_count,
    )


def load_inbox_candidates(root: Path | None = None, *, include_inactive: bool = False) -> tuple[LineageCandidate, ...]:
    payload = _load_inbox_payload(root)
    return _candidates_from_payload(payload, include_inactive=include_inactive)


def load_parent_inbox_candidates(
    root: Path | None = None,
    *,
    include_inactive: bool = False,
    inheritable_only: bool = True,
) -> tuple[LineageCandidate, ...]:
    parent = load_parent(root)
    if parent is None:
        return ()
    return tuple(
        candidate
        for candidate in load_inbox_candidates(root, include_inactive=include_inactive)
        if candidate.repo == parent.repo and (not inheritable_only or is_inheritable_candidate(candidate))
    )


def find_inbox_candidate(candidate_id: str, root: Path | None = None) -> LineageCandidate | None:
    normalized = candidate_id.strip()
    if not normalized:
        return None
    for candidate in load_inbox_candidates(root, include_inactive=True):
        if _candidate_matches_id(candidate, normalized):
            return candidate
    return None


def find_parent_inbox_candidate(candidate_id: str, root: Path | None = None) -> LineageCandidate | None:
    normalized = candidate_id.strip()
    if not normalized:
        return None
    for candidate in load_parent_inbox_candidates(root, include_inactive=True):
        if _candidate_matches_id(candidate, normalized):
            return candidate
    return None


def is_inheritable_candidate(candidate: LineageCandidate) -> bool:
    return candidate.status == STATUS_PENDING and candidate.relevance in INHERITABLE_RELEVANCE


def mark_inbox_candidate(
    candidate_id: str,
    status: str,
    root: Path | None = None,
    *,
    note: str = "",
) -> LineageCandidate:
    if status not in INBOX_STATUSES:
        raise LineageError(f"Unknown ancestor change status: {status}")
    payload = _load_inbox_payload(root)
    candidates = list(_candidates_from_payload(payload, include_inactive=True))
    for index, candidate in enumerate(candidates):
        if _candidate_matches_id(candidate, candidate_id):
            updated = _replace_candidate_review(candidate, status=status, note=note)
            candidates[index] = updated
            payload["candidates"] = [_candidate_to_json(item) for item in sorted(candidates, key=_candidate_sort_key)]
            _save_inbox_payload(payload, root)
            return updated
    raise LineageError(f"Ancestor change {candidate_id} was not found. Run /inherit first.")


def format_lineage(
    chain: tuple[AncestorLink, ...],
    warnings: tuple[str, ...] = (),
    candidates: tuple[LineageCandidate, ...] = (),
    current_agent: CurrentAgentProfile | None = None,
) -> str:
    if not chain and current_agent is None:
        return "\n".join(
            [
                "Ancestor chain: no direct parent configured.",
                f"Add {LINEAGE_PATH.as_posix()} with parent.name, parent.repo, and parent.branch to establish lineage.",
            ]
        )
    counts = _pending_counts_by_ancestor(candidates)
    lines = ["Ancestor chain", ""]
    seen_skills: set[str] = set()
    display_chain = tuple(reversed(chain))
    for index, ancestor in enumerate(display_chain, start=1):
        if _is_root_ancestor_link(ancestor):
            relation = "root ancestor"
        else:
            relation = "parent" if ancestor.depth == 1 else f"ancestor depth {ancestor.depth}"
        count = counts.get(_ancestor_key(ancestor.name, ancestor.repo), 0)
        change_label = "1 change" if count == 1 else f"{count} changes"
        new_skills = tuple(skill for skill in ancestor.skills if skill not in seen_skills)
        seen_skills.update(ancestor.skills)
        skill_label = ", ".join(new_skills) if new_skills else "none"
        if index > 1:
            lines.append("")
        lines.extend(
            [
                f"{index}. {ancestor.name}",
                f"   Relation: {relation}",
                f"   Repo: {ancestor.repo}@{ancestor.branch}",
            ]
        )
        if ancestor.commit_at_birth:
            lines.append(f"   Parent at birth: {ancestor.commit_at_birth[:12]}")
        lines.extend(
            [
                f"   New skills: {skill_label}",
                f"   Pending: {change_label}",
            ]
        )
    if current_agent is not None:
        index = len(display_chain) + 1
        new_skills = tuple(skill for skill in current_agent.skills if skill not in seen_skills)
        skill_label = ", ".join(new_skills) if new_skills else "none"
        if display_chain:
            lines.append("")
        lines.extend(
            [
                f"{index}. {current_agent.name} (current)",
                "   Relation: current agent",
                f"   Source: {current_agent.identity_path.as_posix()}",
                f"   New skills: {skill_label}",
            ]
        )
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def format_refresh_report(report: LineageInboxReport) -> str:
    label = "all ancestors" if report.scope == "all" else "direct parent"
    lines = [f"Ancestor refresh checked {label}."]
    if not report.ancestors:
        lines.append("No ancestors configured.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Checked:")
    lines.extend(
        f"- {item.name} ({item.repo}@{item.branch}) - skills: {', '.join(item.skills) if item.skills else 'unknown'}"
        for item in report.ancestors
    )
    if report.errors:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {error}" for error in report.errors)
    lines.append("")
    if report.new_count == 1:
        lines.append("Added 1 new ancestor change.")
    else:
        lines.append(f"Added {report.new_count} new ancestor changes.")
    lines.append(_format_candidate_list(report.candidates, empty="No pending ancestor changes."))
    lines.extend(
        [
            "",
            "Next:",
            "- /inherit <change_id>",
            "- /inherit all",
            "- /inherit ignore <candidate>",
        ]
    )
    return "\n".join(lines)


def format_parent_inherit_report(report: LineageInboxReport) -> str:
    lines = ["Direct parent inheritance checked."]
    if not report.ancestors:
        lines.append("No direct parent configured.")
        return "\n".join(lines)
    parent = report.ancestors[0]
    lines.extend(["", f"Parent: {parent.name} ({parent.repo}@{parent.branch})"])
    if report.errors:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {error}" for error in report.errors)
    lines.append("")
    if report.new_count == 1:
        lines.append("Added 1 new direct-parent change.")
    else:
        lines.append(f"Added {report.new_count} new direct-parent changes.")
    inheritable = tuple(candidate for candidate in report.candidates if is_inheritable_candidate(candidate))
    skipped = len(report.candidates) - len(inheritable)
    lines.append(format_parent_inbox(inheritable))
    if skipped:
        lines.append(f"Filtered out {skipped} parent change(s) that are not inheritable candidates.")
    lines.extend(
        [
            "",
            "Next:",
            "- /inherit <change_id>",
            "- /inherit all",
            "- /inherit ignore <candidate>",
        ]
    )
    return "\n".join(lines)


def format_inbox(candidates: tuple[LineageCandidate, ...]) -> str:
    return "\n".join(
        [
            "Ancestor changes:",
            _format_candidate_list(candidates, empty="No pending ancestor changes."),
        ]
    )


def format_parent_inbox(candidates: tuple[LineageCandidate, ...]) -> str:
    return "\n".join(
        [
            "Direct parent inheritance candidates:",
            _format_candidate_list(candidates, empty="No pending direct-parent inheritance candidates."),
        ]
    )


def format_candidate(candidate: LineageCandidate) -> str:
    labels = ", ".join(candidate.labels) if candidate.labels else "none"
    files = "\n".join(f"- {path}" for path in candidate.files) or "- unavailable"
    time_label = "Merged at" if candidate.pr_number else "Committed at"
    commit_label = "Merge commit" if candidate.pr_number else "Commit"
    return "\n".join(
        [
            f"{candidate.id} {candidate.title}",
            f"Status: {candidate.status}",
            f"Repo: {candidate.repo}",
            f"Ancestor: {candidate.ancestor_name} (depth {candidate.depth})",
            f"URL: {candidate.url or 'unavailable'}",
            f"{time_label}: {candidate.merged_at or 'unknown'}",
            f"{commit_label}: {candidate.merge_commit or 'unknown'}",
            f"Labels: {labels}",
            f"Relevance: {candidate.relevance}",
            f"Confidence: {candidate.confidence}",
            f"Reason: {candidate.reason}",
            "",
            "Body excerpt:",
            candidate.body_excerpt or "No PR body excerpt was recorded.",
            "",
            "Files:",
            files,
        ]
    )


def lineage_candidate_context(candidate: LineageCandidate) -> str:
    return "\n".join(
        [
            "Ancestor change context:",
            format_candidate(candidate),
            "",
            "Use this as repository context only. Inspect local files before deciding whether Enoch should adapt it.",
        ]
    )


def lineage_adopt_prompt(candidate: LineageCandidate) -> str:
    return "\n".join(
        [
            "Consider whether Enoch should adapt this ancestor change.",
            "If it is useful for Enoch, request a focused repository edit.",
            "If it is not useful, explain why and do not request an edit.",
            "",
            lineage_candidate_context(candidate),
        ]
    )


def _candidate_from_pr(
    ancestor: AncestorLink,
    pr: dict[str, Any],
    files: tuple[str, ...],
    settings: LineageSettings,
) -> LineageCandidate:
    number = int(pr.get("number") or 0)
    labels = tuple(str(item.get("name") or "") for item in pr.get("labels", []) if item.get("name"))
    title = str(pr.get("title") or "").strip() or f"PR #{number}"
    relevance, confidence, reason = _rank_candidate(title, labels, files, settings)
    body = str(pr.get("body") or "").strip()
    return LineageCandidate(
        id=f"{ancestor.repo}#{number}",
        repo=ancestor.repo,
        pr_number=number,
        title=title,
        url=str(pr.get("url") or ""),
        merged_at=str(pr.get("mergedAt") or ""),
        merge_commit=_merge_commit_sha(pr),
        ancestor_name=ancestor.name,
        depth=ancestor.depth,
        labels=labels,
        files=files,
        relevance=relevance,
        confidence=confidence,
        reason=reason,
        body_excerpt=_excerpt(body),
    )


def _candidate_from_commit(
    ancestor: AncestorLink,
    commit: dict[str, Any],
    files: tuple[str, ...],
    settings: LineageSettings,
) -> LineageCandidate:
    sha = _commit_sha(commit)
    title = _commit_title(commit)
    relevance, confidence, reason = _rank_candidate(title, (), files, settings)
    body = _commit_message(commit)
    return LineageCandidate(
        id=f"{ancestor.repo}@{sha[:12]}",
        repo=ancestor.repo,
        pr_number=0,
        title=title,
        url=str(commit.get("html_url") or ""),
        merged_at=_commit_date(commit),
        merge_commit=sha,
        ancestor_name=ancestor.name,
        depth=ancestor.depth,
        labels=(),
        files=files,
        relevance=relevance,
        confidence=confidence,
        reason=reason,
        body_excerpt=_excerpt(body),
    )


def _candidate_matches_id(candidate: LineageCandidate, candidate_id: str) -> bool:
    normalized = candidate_id.strip()
    if candidate.pr_number:
        return candidate.id == normalized or f"#{candidate.pr_number}" == normalized or str(candidate.pr_number) == normalized
    sha = candidate.merge_commit
    return candidate.id == normalized or sha == normalized or bool(sha and sha.startswith(normalized))


def _candidate_matches_ancestor(candidate: LineageCandidate, ancestor: str) -> bool:
    wanted = _normalize_ancestor_ref(ancestor)
    aliases = {
        _normalize_ancestor_ref(candidate.ancestor_name),
        _normalize_ancestor_ref(candidate.repo),
        _normalize_ancestor_ref(candidate.repo.rsplit("/", 1)[-1]),
    }
    return wanted in aliases


def _pending_counts_by_ancestor(candidates: tuple[LineageCandidate, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.status != STATUS_PENDING:
            continue
        key = _ancestor_key(candidate.ancestor_name, candidate.repo)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _ancestor_key(name: str, repo: str) -> str:
    return f"{_normalize_ancestor_ref(name)}|{_normalize_ancestor_ref(repo)}"


def _normalize_ancestor_ref(value: str) -> str:
    return value.strip().lower()


def _rank_candidate(
    title: str,
    labels: tuple[str, ...],
    files: tuple[str, ...],
    settings: LineageSettings,
) -> tuple[str, str, str]:
    lowered_title = title.lower()
    lowered_labels = {label.lower() for label in labels}
    if "inherit:blocked" in lowered_labels:
        return "blocked", "high", "PR is explicitly labeled as blocked for inheritance."
    if any(label.startswith("inherit:") for label in lowered_labels):
        return "high", "high", "PR has an inheritance label."
    if any(word in lowered_title for word in settings.important_title_words):
        return "high", "medium", "Title suggests a bug, security, runtime, or update fix."
    if any(path.startswith(settings.important_file_prefixes) for path in files):
        return "medium", "medium", "Changed files touch shared agent runtime or command code."
    return "low", "low", "No strong inheritance signal was detected from metadata."


def _merge_commit_sha(pr: dict[str, Any]) -> str:
    merge_commit = pr.get("mergeCommit") or {}
    if isinstance(merge_commit, dict):
        return str(merge_commit.get("oid") or merge_commit.get("sha") or "")
    return str(merge_commit or "")


def _commit_sha(commit: dict[str, Any]) -> str:
    return str(commit.get("sha") or "").strip()


def _commit_message(commit: dict[str, Any]) -> str:
    data = commit.get("commit") or {}
    return str(data.get("message") or "").strip()


def _commit_title(commit: dict[str, Any]) -> str:
    message = _commit_message(commit)
    return message.splitlines()[0].strip() if message else f"Commit {_commit_sha(commit)[:12]}"


def _commit_date(commit: dict[str, Any]) -> str:
    data = commit.get("commit") or {}
    committer = data.get("committer") or {}
    author = data.get("author") or {}
    return str(committer.get("date") or author.get("date") or "")


def _candidate_to_json(candidate: LineageCandidate) -> dict[str, Any]:
    data = asdict(candidate)
    data["labels"] = list(candidate.labels)
    data["files"] = list(candidate.files)
    return data


def _candidate_from_json(data: dict[str, Any]) -> LineageCandidate:
    status = str(data.get("status") or STATUS_PENDING)
    if status not in INBOX_STATUSES:
        status = STATUS_PENDING
    return LineageCandidate(
        id=str(data["id"]),
        repo=str(data["repo"]),
        pr_number=int(data["pr_number"]),
        title=str(data["title"]),
        url=str(data.get("url") or ""),
        merged_at=str(data.get("merged_at") or ""),
        merge_commit=str(data.get("merge_commit") or ""),
        ancestor_name=str(data.get("ancestor_name") or ""),
        depth=int(data.get("depth") or 0),
        labels=tuple(str(item) for item in data.get("labels", [])),
        files=tuple(str(item) for item in data.get("files", [])),
        relevance=str(data.get("relevance") or "unknown"),
        confidence=str(data.get("confidence") or "unknown"),
        reason=str(data.get("reason") or ""),
        body_excerpt=str(data.get("body_excerpt") or ""),
        status=status,
        first_seen_at=str(data.get("first_seen_at") or ""),
        last_seen_at=str(data.get("last_seen_at") or ""),
        reviewed_at=str(data.get("reviewed_at") or ""),
        review_note=str(data.get("review_note") or ""),
    )


def _load_inbox_payload(root: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads(lineage_inbox_file(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": INBOX_SCHEMA_VERSION, "candidates": []}
    if not isinstance(data, dict):
        return {"schema_version": INBOX_SCHEMA_VERSION, "candidates": []}
    data.setdefault("schema_version", INBOX_SCHEMA_VERSION)
    data.setdefault("candidates", [])
    return data


def _save_inbox_payload(payload: dict[str, Any], root: Path | None = None) -> None:
    path = lineage_inbox_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _candidates_from_payload(payload: dict[str, Any], *, include_inactive: bool) -> tuple[LineageCandidate, ...]:
    candidates = []
    for item in payload.get("candidates", []):
        if not isinstance(item, dict):
            continue
        try:
            candidate = _candidate_from_json(item)
        except (KeyError, TypeError, ValueError):
            continue
        if include_inactive or candidate.status == STATUS_PENDING:
            candidates.append(candidate)
    return tuple(sorted(candidates, key=_candidate_sort_key))


def _merge_candidate_metadata(
    candidate: LineageCandidate,
    previous: LineageCandidate | None,
    refreshed_at: str,
) -> LineageCandidate:
    if previous is None:
        return LineageCandidate(
            **{
                **_candidate_to_json(candidate),
                "labels": candidate.labels,
                "files": candidate.files,
                "first_seen_at": refreshed_at,
                "last_seen_at": refreshed_at,
            }
        )
    return LineageCandidate(
        **{
            **_candidate_to_json(candidate),
            "labels": candidate.labels,
            "files": candidate.files,
            "status": previous.status,
            "first_seen_at": previous.first_seen_at or refreshed_at,
            "last_seen_at": refreshed_at,
            "reviewed_at": previous.reviewed_at,
            "review_note": previous.review_note,
        }
    )


def _replace_candidate_review(candidate: LineageCandidate, *, status: str, note: str) -> LineageCandidate:
    return LineageCandidate(
        **{
            **_candidate_to_json(candidate),
            "labels": candidate.labels,
            "files": candidate.files,
            "status": status,
            "reviewed_at": _now(),
            "review_note": note.strip(),
        }
    )


def _format_candidate_list(candidates: tuple[LineageCandidate, ...], *, empty: str) -> str:
    if not candidates:
        return empty
    lines = ["Pending ancestor changes:"]
    for candidate in candidates:
        files = ", ".join(candidate.files[:3]) if candidate.files else "files unavailable"
        lines.extend(
            [
                f"- {candidate.id} {candidate.title}",
                f"  Relevance: {candidate.relevance}; confidence: {candidate.confidence}",
                f"  Reason: {candidate.reason}",
                f"  Files: {files}",
            ]
        )
    return "\n".join(lines)


def _candidate_sort_key(candidate: LineageCandidate) -> tuple[int, int, str, int]:
    status_order = {STATUS_PENDING: 0, STATUS_IGNORED: 1, STATUS_ADOPTED: 2}
    relevance_order = {"high": 0, "medium": 1, "low": 2, "blocked": 3}
    return (
        status_order.get(candidate.status, 9),
        relevance_order.get(candidate.relevance, 9),
        candidate.repo,
        candidate.pr_number,
    )


def _clean_yaml_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    return cleaned


def _normalize_repo(repo: str) -> str:
    value = repo.strip()
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.netloc.lower() == "github.com":
            value = parsed.path.lstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.strip("/")


def _excerpt(text: str, limit: int = 700) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}..."


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
