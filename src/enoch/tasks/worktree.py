from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from enoch.vcs_tools import (
    VcsError,
    branch_exists,
    changed_files,
    create_workspace,
    current_branch,
    delete_branch,
    remove_workspace,
    workspace_paths,
)


@dataclass(frozen=True)
class TaskWorktree:
    task_id: int
    path: Path
    branch: str
    created: bool


@dataclass(frozen=True)
class TaskWorktreeState:
    task_id: int
    path: Path
    branch: str
    changed_files: tuple[str, ...] = ()
    inspection_error: str = ""

    @property
    def clean(self) -> bool:
        return not self.changed_files and not self.inspection_error


def task_worktree_path(control_root: Path, task_id: int) -> Path:
    resolved = control_root.resolve()
    instance_key = sha256(str(resolved).encode("utf-8")).hexdigest()[:10]
    return resolved.parent / ".enoch-task-worktrees" / f"{resolved.name}-{instance_key}" / f"task-{task_id}"


def task_worktree_root(control_root: Path) -> Path:
    return task_worktree_path(control_root, 0).parent


def task_branch_name(
    task_id: int,
    request: str,
    *,
    resident_branch: str = "",
    created_at: str = "",
) -> str:
    owner = _slug(resident_branch.removeprefix("agent/")) or "enoch"
    request_slug = _slug(request)[:32] or "work"
    identity = sha256(f"{resident_branch}\0{created_at}\0{task_id}".encode("utf-8")).hexdigest()[:8]
    return f"enoch/{owner}-task-{task_id}-{identity}-{request_slug}"


def list_task_worktrees(control_root: Path) -> tuple[TaskWorktreeState, ...]:
    namespace = task_worktree_root(control_root)
    states: list[TaskWorktreeState] = []
    for path in workspace_paths(control_root):
        task_id = _task_id_from_path(path, namespace)
        if task_id is None:
            continue
        try:
            branch = current_branch(path)
            files = tuple(sorted(changed_files(path)))
            error = ""
        except (VcsError, OSError) as exc:
            branch = ""
            files = ()
            error = str(exc)
        states.append(
            TaskWorktreeState(
                task_id=task_id,
                path=path,
                branch=branch,
                changed_files=files,
                inspection_error=error,
            )
        )
    return tuple(sorted(states, key=lambda state: state.task_id))


def task_worktree_state(control_root: Path, task_id: int) -> TaskWorktreeState | None:
    return next(
        (state for state in list_task_worktrees(control_root) if state.task_id == task_id),
        None,
    )


def remove_managed_task_worktree(
    control_root: Path,
    task_id: int,
    *,
    discard: bool = False,
) -> str:
    state = task_worktree_state(control_root, task_id)
    if state is None:
        raise VcsError(f"Task #{task_id} has no registered task worktree.")
    if not discard and not state.clean:
        detail = (
            f"changed files: {', '.join(state.changed_files)}"
            if state.changed_files
            else f"inspection failed: {state.inspection_error}"
        )
        raise VcsError(
            f"Task #{task_id} worktree is not clean ({detail}). "
            f"Inspect it with /worktree show {task_id}. "
            f"To permanently discard it, use /worktree discard {task_id} force."
        )

    remove_workspace(state.path, control_root, force=discard)
    messages = [f"Removed task #{task_id} worktree {state.path}."]
    if state.branch:
        try:
            delete_branch(state.branch, control_root, force=discard)
        except VcsError as error:
            messages.append(f"Kept local branch {state.branch}: {error}")
        else:
            messages.append(f"Deleted local branch {state.branch}.")
    return "\n".join(messages)


def prepare_task_worktree(
    control_root: Path,
    task_id: int,
    request: str,
    *,
    start_point: str,
    resident_branch: str = "",
    created_at: str = "",
    existing_path: str = "",
    existing_branch: str = "",
) -> TaskWorktree:
    path = Path(existing_path).expanduser().resolve() if existing_path else task_worktree_path(control_root, task_id)
    registered = set(workspace_paths(control_root))
    if path in registered:
        branch = current_branch(path)
        if existing_branch and branch != existing_branch:
            raise VcsError(
                f"Task #{task_id} worktree is on {branch}, expected {existing_branch}."
            )
        return TaskWorktree(task_id=task_id, path=path, branch=branch, created=False)

    if path.exists() and any(path.iterdir()):
        raise VcsError(f"Task #{task_id} worktree path is not empty: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = existing_branch or task_branch_name(
        task_id,
        request,
        resident_branch=resident_branch,
        created_at=created_at,
    )
    exists = branch_exists(branch, control_root)
    create_workspace(
        path,
        branch,
        control_root,
        start_point="" if exists else start_point,
        create_branch=not exists,
    )
    return TaskWorktree(task_id=task_id, path=path, branch=branch, created=True)


def prepare_existing_branch_worktree(
    control_root: Path,
    task_id: int,
    branch: str,
    *,
    existing_path: str = "",
) -> TaskWorktree:
    path = Path(existing_path).expanduser().resolve() if existing_path else task_worktree_path(control_root, task_id)
    registered = set(workspace_paths(control_root))
    if path in registered:
        checked_out = current_branch(path)
        if checked_out != branch:
            raise VcsError(
                f"Task #{task_id} worktree is on {checked_out}, expected {branch}."
            )
        return TaskWorktree(task_id=task_id, path=path, branch=branch, created=False)
    if path.exists() and any(path.iterdir()):
        raise VcsError(f"Task #{task_id} worktree path is not empty: {path}")
    if not branch_exists(branch, control_root):
        raise VcsError(f"Local branch {branch} does not exist.")
    path.parent.mkdir(parents=True, exist_ok=True)
    create_workspace(path, branch, control_root)
    return TaskWorktree(task_id=task_id, path=path, branch=branch, created=True)


def remove_task_worktree(
    control_root: Path,
    worktree: TaskWorktree,
    *,
    delete_local_branch: bool = True,
    force_delete_branch: bool = False,
) -> str:
    remove_workspace(worktree.path, control_root)
    messages = [f"Removed task #{worktree.task_id} worktree."]
    if delete_local_branch:
        try:
            delete_branch(
                worktree.branch,
                control_root,
                force=force_delete_branch,
            )
        except VcsError as error:
            messages.append(f"Kept local branch {worktree.branch}: {error}")
        else:
            messages.append(f"Deleted local branch {worktree.branch}.")
    return "\n".join(messages)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _task_id_from_path(path: Path, namespace: Path) -> int | None:
    try:
        relative = path.resolve().relative_to(namespace.resolve())
    except ValueError:
        return None
    if len(relative.parts) != 1:
        return None
    match = re.fullmatch(r"task-(\d+)", relative.name)
    if match is None:
        return None
    task_id = int(match.group(1))
    return task_id if task_id > 0 else None
