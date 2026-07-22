from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None

from enoch.identity import Identity
from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 1
SUMMARY_LIMIT = 4000
_LEARNING_THREAD_LOCK = threading.RLock()
_PR_URL_PATTERN = re.compile(r"https://[^\s]+/(?:pull|pulls|merge_requests)/\d+")


@dataclass(frozen=True)
class LearningArtifact:
    id: str
    artifact_type: str
    source_agent: str
    created_at: str
    task_id: int | None
    command: str
    request: str
    result_summary: str
    pr_urls: tuple[str, ...]
    changed_files: tuple[str, ...]
    skill_names: tuple[str, ...]
    context_source: str = ""


def learning_dir(root: Path | None = None) -> Path:
    return enoch_home(root) / "learning"


def learning_index_path(root: Path | None = None) -> Path:
    return learning_dir(root) / "artifacts.jsonl"


def learning_artifact_path(artifact_id: str, root: Path | None = None) -> Path:
    return learning_dir(root) / "artifacts" / f"{artifact_id}.md"


def record_learning_artifact(
    identity: Identity,
    *,
    request: str,
    result: str,
    root: Path | None = None,
    task_id: int | None = None,
    command: str = "",
    context_source: str = "",
    pr_urls: tuple[str, ...] = (),
    changed_files: tuple[str, ...] = (),
) -> LearningArtifact | None:
    cleaned_request = " ".join(request.split())
    if not cleaned_request:
        raise ValueError("Learning artifact request is required.")
    created_at = current_time()
    result_summary = _clip(result)
    urls = _dedupe((*pr_urls, *_PR_URL_PATTERN.findall(result)))
    files = _dedupe((*changed_files, *_changed_files_from_result(result)))
    skill_names = tuple(_skill_names(files))
    if not skill_names:
        return None
    artifact = LearningArtifact(
        id=_artifact_id(created_at, identity.name, cleaned_request),
        artifact_type="skill",
        source_agent=identity.name,
        created_at=created_at,
        task_id=task_id,
        command=command.strip(),
        request=cleaned_request,
        result_summary=result_summary,
        pr_urls=tuple(urls),
        changed_files=tuple(files),
        skill_names=skill_names,
        context_source=context_source.strip(),
    )
    with _learning_transaction(root):
        _append_index(artifact, root)
        atomic_write(learning_artifact_path(artifact.id, root), _artifact_markdown(artifact))
    return artifact


def _append_index(artifact: LearningArtifact, root: Path | None = None) -> None:
    path = learning_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"schema_version": SCHEMA_VERSION, **asdict(artifact)}, sort_keys=True) + "\n")


def _artifact_markdown(artifact: LearningArtifact) -> str:
    lines = [
        f"# Learning Artifact {artifact.id}",
        "",
        f"- Type: {artifact.artifact_type}",
        f"- Source agent: {artifact.source_agent}",
        f"- Created at: {artifact.created_at}",
        f"- Command: {artifact.command or 'unknown'}",
        f"- Task ID: {artifact.task_id if artifact.task_id is not None else 'none'}",
        f"- Context source: {artifact.context_source or 'none'}",
        f"- Skills: {', '.join(artifact.skill_names)}",
        "",
        "## Request",
        "",
        artifact.request,
        "",
        "## Result Summary",
        "",
        artifact.result_summary or "No result summary recorded.",
        "",
        "## Pull Requests",
        "",
    ]
    if artifact.pr_urls:
        lines.extend(f"- {url}" for url in artifact.pr_urls)
    else:
        lines.append("- none")
    lines.extend(["", "## Changed Files", ""])
    if artifact.changed_files:
        lines.extend(f"- {path}" for path in artifact.changed_files)
    else:
        lines.append("- unknown")
    lines.extend(
        [
            "",
            "## Inheritance Notes",
            "",
            "This skill artifact was recorded automatically after successful work that changed an agent skill package. Descendant agents should inspect the skill, adapt useful ideas to their own body, and run their own tests before inheriting behavior.",
            "",
        ]
    )
    return "\n".join(lines)


def _skill_names(paths: list[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for path in paths:
        parts = Path(path).parts
        for index, part in enumerate(parts):
            if part == "skills" and index + 1 < len(parts):
                name = parts[index + 1].strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
                break
    return names


def _changed_files_from_result(result: str) -> tuple[str, ...]:
    files: list[str] = []
    in_files = False
    for raw_line in result.splitlines():
        line = raw_line.strip()
        if line == "Files:":
            in_files = True
            continue
        if in_files and line.startswith("- "):
            files.append(line[2:].strip())
            continue
        if in_files and line and not line.startswith("- "):
            in_files = False
    return tuple(files)


def _artifact_id(created_at: str, agent: str, request: str) -> str:
    digest = hashlib.sha1(f"{created_at}\0{agent}\0{request}".encode("utf-8")).hexdigest()[:10]
    return f"{created_at.replace(':', '').replace('+', 'Z')}-{digest}"


def _clip(text: str, limit: int = SUMMARY_LIMIT) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}\n\n[truncated]"


def _dedupe(items: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return output


@contextmanager
def _learning_transaction(root: Path | None = None):
    path = learning_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _LEARNING_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
