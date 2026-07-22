from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from enoch.lineage.core import LINEAGE_PATH, ParentLink, load_parent, parse_lineage_parent
from enoch.providers.registry import ProviderError, configure_provider


def setup_command(
    text: str,
    root: Path,
    *,
    prompt=None,
    prefix: str = "",
) -> str:
    parts = text.split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) >= 2 else ""
    argument = parts[2].strip() if len(parts) >= 3 else ""
    if subcommand in {"ancestor", "parent", "lineage"}:
        return ancestor_setup_command(argument, root, prefix=prefix)
    provider_text = text if parts and parts[0].lower().startswith("setup-") else " ".join(parts[1:])
    try:
        return configure_provider(
            "chat",
            provider_text,
            root,
            prompt=prompt,
            prefix=prefix,
        )
    except ProviderError as error:
        return f"Enoch could not configure the chat provider: {error}"


def ancestor_setup_command(argument: str, root: Path, *, prefix: str = "") -> str:
    parts = argument.split()
    action = parts[0].lower() if parts else ""
    if action in {"show", "status"}:
        parent = load_parent(root)
        if parent is None:
            return f"No lineage parent configured. Use {_command(prefix, 'setup ancestor <repo-url>')}."
        return f"Lineage parent: {parent.name} ({parent.repo}@{parent.branch})"
    if action in {"clear", "remove", "none"}:
        path = root / LINEAGE_PATH
        try:
            path.unlink()
        except FileNotFoundError:
            return "No lineage parent was configured."
        return f"Removed lineage parent from {path}."
    spec = _parse_ancestor_link(parts)
    if spec is None:
        return f"Use {_command(prefix, 'setup ancestor <repo-url>')}."
    return save_ancestor(spec.name, spec.repo, spec.branch, root)


def save_ancestor(name: str, repo: str, branch: str, root: Path) -> str:
    name = name.strip()
    repo = repo.strip()
    branch = branch.strip() or "main"
    if not name or not repo:
        return "Lineage parent needs both a name and repo."
    text = "\n".join(
        [
            "parent:",
            f"  name: {name}",
            f"  repo: {repo}",
            f"  branch: {branch}",
            "",
        ]
    )
    parent = parse_lineage_parent(text)
    if parent is None:
        return "Enoch could not parse that lineage parent."
    path = root / LINEAGE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "\n".join(
        [
            f"Lineage parent saved to {path}.",
            f"Parent: {parent.name} ({parent.repo}@{parent.branch})",
            "This is repo-side lineage metadata. Commit it with the descendant agent.",
        ]
    )


class _AncestorSpec:
    def __init__(self, name: str, repo: str, branch: str) -> None:
        self.name = name
        self.repo = repo
        self.branch = branch


def _parse_ancestor_link(parts: list[str]) -> _AncestorSpec | None:
    if len(parts) != 1:
        return None
    link = parts[0]
    if not _is_repo_url(link):
        return None
    repo = _normalize_repo(link)
    if not repo:
        return None
    return _AncestorSpec(name=_infer_ancestor_name(repo), repo=repo, branch="main")


def _is_repo_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_repo(value: str) -> str:
    value = value.strip().removesuffix(".git")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.netloc:
        segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) >= 2:
            return f"{segments[0]}/{segments[1].removesuffix('.git')}"
        return ""
    if value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    segments = [segment for segment in value.strip("/").split("/") if segment]
    if len(segments) == 2:
        return f"{segments[0]}/{segments[1]}"
    return ""


def _infer_ancestor_name(repo: str) -> str:
    repo_name = repo.split("/", 1)[-1]
    words = [word for word in repo_name.replace("_", "-").split("-") if word]
    if not words:
        return repo_name or "Ancestor"
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _parent_status(parent: ParentLink | None) -> str:
    if parent is None:
        return "not set"
    return f"{parent.name} ({parent.repo}@{parent.branch})"


def _command(prefix: str, text: str) -> str:
    if prefix:
        return f"{prefix}{text}"
    return f"bin/enoch {text}"
