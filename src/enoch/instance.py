from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

from enoch.identity import Identity


DEFAULT_INSTANCE_NAME = "default"


class InstanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstanceInitResult:
    agent_name: str
    instance_name: str
    worktree: Path
    created_worktree: bool
    branch: str
    metadata_path: Path


def init_instance(
    identity: Identity,
    root: Path,
    *,
    instance_name: str = DEFAULT_INSTANCE_NAME,
    worktree: Path | None = None,
    branch: str = "",
) -> InstanceInitResult:
    root = root.resolve()
    instance_name = _clean_instance_name(instance_name)
    target = worktree.expanduser().resolve() if worktree is not None else root
    created_worktree = False
    if target != root:
        created_worktree = True
        branch = branch.strip() or _default_instance_branch(identity.body.package, instance_name)
        _create_git_worktree(root, target, branch)
    elif not branch.strip():
        branch = _current_branch(root)
    metadata_path = _write_instance_metadata(identity, root, target, instance_name, branch)
    return InstanceInitResult(
        agent_name=identity.name,
        instance_name=instance_name,
        worktree=target,
        created_worktree=created_worktree,
        branch=branch,
        metadata_path=metadata_path,
    )


def format_instance_init_result(result: InstanceInitResult) -> str:
    action = "Created worktree instance" if result.created_worktree else "Initialized current worktree instance"
    return "\n".join(
        [
            f"{action} for {result.agent_name}.",
            f"Instance: {result.instance_name}",
            f"Worktree: {result.worktree}",
            f"Branch: {result.branch}",
            f"Metadata: {result.metadata_path}",
        ]
    )


def instance_branch(root: Path) -> str:
    path = root / ".agent" / "instance.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    in_worktree = False
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            in_worktree = line.strip() == "worktree:"
            continue
        if not in_worktree or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == "branch":
            return _clean_yaml_value(value)
    return ""


def _create_git_worktree(root: Path, target: Path, branch: str) -> None:
    if target.exists() and any(target.iterdir()):
        raise InstanceError(f"Worktree target is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = _branch_exists(root, branch)
    args = ["worktree", "add"]
    if not branch_exists:
        args.extend(["-b", branch])
    args.extend([str(target), branch if branch_exists else "HEAD"])
    _git(args, root)


def _write_instance_metadata(
    identity: Identity,
    source_root: Path,
    target: Path,
    instance_name: str,
    branch: str,
) -> Path:
    path = target / ".agent" / "instance.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "schema_version: 1",
                "agent:",
                f"  name: {_yaml_quote(identity.name)}",
                f"  package: {_yaml_quote(identity.body.package)}",
                f"  generation: {identity.generation}",
                "instance:",
                f"  name: {_yaml_quote(instance_name)}",
                f"  created_at: {_yaml_quote(_now())}",
                "worktree:",
                f"  path: {_yaml_quote(str(target))}",
                f"  source_repo: {_yaml_quote(str(source_root))}",
                f"  branch: {_yaml_quote(branch)}",
                "  kind: agent-instance",
                "state:",
                f"  home: {_yaml_quote('.' + identity.body.package)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _clean_instance_name(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned or DEFAULT_INSTANCE_NAME


def _default_instance_branch(package: str, instance_name: str) -> str:
    return f"agent/{package}-{_slug(instance_name)}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or DEFAULT_INSTANCE_NAME


def _branch_exists(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _current_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "HEAD"


def _git(args: list[str], root: Path) -> None:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise InstanceError(detail)


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _clean_yaml_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
