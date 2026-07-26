from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from enoch.config import _parse_simple_yaml
from enoch.paths import private_state_path, storage_layout
from enoch.state import (
    StateCorruptionError,
    atomic_write,
    file_transaction,
    load_json_object,
)


PRIVATE_STATE_MANIFEST_SCHEMA_VERSION = 1
PRIVATE_STATE_VERSION = 1
MANIFEST_NAME = "state_manifest.json"
BACKUP_DIRECTORY = "backups"


class PrivateStateError(RuntimeError):
    """Base error for private-state validation and migration."""


class UnsupportedPrivateStateError(PrivateStateError):
    """Raised when state is corrupt or newer than this Enoch build."""


class PrivateStateMigrationError(PrivateStateError):
    """Raised when a migration cannot complete or roll back safely."""


@dataclass(frozen=True)
class StateFileSchema:
    pattern: str
    current_version: int
    defaults: tuple[tuple[str, object], ...] = ()
    fields: tuple[tuple[str, tuple[type, ...]], ...] = ()
    embedded_version: bool = True


@dataclass(frozen=True)
class StateMigrationAction:
    relative_path: str
    from_version: int
    to_version: int


@dataclass(frozen=True)
class PrivateStatePlan:
    manifest_status: str
    files_checked: int
    actions: tuple[StateMigrationAction, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def migration_required(self) -> bool:
        return self.manifest_status != "current" or bool(self.actions)


@dataclass(frozen=True)
class PrivateStateMigrationResult:
    dry_run: bool
    applied: bool
    plan: PrivateStatePlan
    backup_path: Path | None = None


STATE_FILE_SCHEMAS = (
    StateFileSchema(
        "task_queue.json",
        11,
        (
            ("next_id", 1),
            ("pending", []),
            ("paused", []),
            ("running", None),
            ("history", []),
        ),
        (
            ("next_id", (int,)),
            ("pending", (list,)),
            ("paused", (list,)),
            ("running", (dict, type(None))),
            ("history", (list,)),
        ),
    ),
    StateFileSchema(
        "backlog.json",
        2,
        (("next_id", 1), ("pending", []), ("history", [])),
        (("next_id", (int,)), ("pending", (list,)), ("history", (list,))),
    ),
    StateFileSchema(
        "cron.json",
        3,
        (("next_id", 1), ("active", []), ("history", [])),
        (("next_id", (int,)), ("active", (list,)), ("history", (list,))),
    ),
    StateFileSchema(
        "evolve.json",
        2,
        (
            ("mode", "co-evolve"),
            ("theme", ""),
            ("schedule_enabled", False),
            ("schedule_interval_seconds", 0),
        ),
        (
            ("mode", (str,)),
            ("theme", (str,)),
            ("schedule_enabled", (bool,)),
            ("schedule_interval_seconds", (int,)),
        ),
    ),
    StateFileSchema(
        "evolve_candidates.json",
        4,
        (("candidates", []),),
        (("candidates", (list,)),),
    ),
    StateFileSchema(
        "evolve_brainstorm_fallback.json",
        1,
        (("attempts", {}),),
        (("attempts", (dict,)),),
    ),
    StateFileSchema(
        "pending_evolve_adoptions.json",
        1,
        (("adoptions", []),),
        (("adoptions", (list,)),),
    ),
    StateFileSchema(
        "codex_sessions.json",
        1,
        (("sessions", {}),),
        (("sessions", (dict,)),),
    ),
    StateFileSchema(
        "daemon_epoch.json",
        1,
        (("current", None),),
        (("current", (dict, type(None))),),
    ),
    StateFileSchema(
        "memory/long_term.json",
        1,
        (("memories", []),),
        (("memories", (list,)),),
    ),
    StateFileSchema("last_codex_input.json", 1),
    StateFileSchema(
        "channels/*/cursor.json",
        1,
        (("cursor", None),),
        (("cursor", (int, str, type(None))),),
    ),
    StateFileSchema("channels/*/lifecycle.json", 1),
    StateFileSchema(
        "channels/*/inbox.json",
        1,
        (("events", {}),),
        (("events", (dict,)),),
    ),
    StateFileSchema(
        "channels/*/notifications.json",
        1,
        (("notifications", {}),),
        (("notifications", (dict,)),),
    ),
    StateFileSchema("config.yaml", 1, embedded_version=False),
)


def private_state_manifest_path(root: Path | None = None) -> Path:
    return private_state_path(MANIFEST_NAME, root)


def plan_private_state(root: Path | None = None) -> PrivateStatePlan:
    layout = storage_layout(root)
    manifest_status, manifest_errors = _manifest_status(root)
    errors = list(manifest_errors)
    actions: list[StateMigrationAction] = []
    files_checked = 0
    seen: set[Path] = set()
    for schema in STATE_FILE_SCHEMAS:
        for path in sorted(layout.private_state.glob(schema.pattern)):
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            if not layout.contains("private-state", resolved):
                errors.append(f"{path}: path crosses the private-state boundary")
                continue
            files_checked += 1
            try:
                version = _state_file_version(path, schema)
            except (OSError, StateCorruptionError, ValueError) as error:
                errors.append(str(error))
                continue
            if version > schema.current_version:
                errors.append(
                    f"{path}: unsupported schema version {version}; "
                    f"this Enoch supports {schema.current_version}"
                )
                continue
            try:
                _normalized_payload(path, schema)
            except (OSError, StateCorruptionError, ValueError) as error:
                errors.append(str(error))
                continue
            if version < schema.current_version:
                actions.append(
                    StateMigrationAction(
                        relative_path=path.relative_to(layout.private_state).as_posix(),
                        from_version=version,
                        to_version=schema.current_version,
                    )
                )
    return PrivateStatePlan(
        manifest_status=manifest_status,
        files_checked=files_checked,
        actions=tuple(actions),
        errors=tuple(errors),
    )


def assert_private_state_supported(root: Path | None = None) -> PrivateStatePlan:
    plan = plan_private_state(root)
    if not plan.valid:
        raise UnsupportedPrivateStateError(
            "Private state is not supported by this Enoch build:\n- "
            + "\n- ".join(plan.errors)
        )
    return plan


def migrate_private_state(
    root: Path | None = None,
    *,
    dry_run: bool = False,
) -> PrivateStateMigrationResult:
    plan = assert_private_state_supported(root)
    if dry_run or not plan.migration_required:
        return PrivateStateMigrationResult(
            dry_run=dry_run,
            applied=False,
            plan=plan,
        )
    _require_daemon_stopped(root)
    layout = storage_layout(root)
    manifest_path = private_state_manifest_path(root)
    with file_transaction(manifest_path):
        locked_plan = assert_private_state_supported(root)
        if not locked_plan.migration_required:
            return PrivateStateMigrationResult(
                dry_run=False,
                applied=False,
                plan=locked_plan,
            )
        backup_path = _create_backup(locked_plan, root)
        manifest_existed = manifest_path.exists()
        try:
            for action in locked_plan.actions:
                path = layout.private_path(action.relative_path)
                schema = _schema_for_relative_path(action.relative_path)
                if schema is None:
                    raise PrivateStateMigrationError(
                        f"No migration schema is registered for {action.relative_path}."
                    )
                payload = _normalized_payload(path, schema)
                atomic_write(
                    path,
                    json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
                    + "\n",
                )
            atomic_write(
                manifest_path,
                json.dumps(_current_manifest(), indent=2, sort_keys=True) + "\n",
            )
            completed = assert_private_state_supported(root)
            if completed.migration_required:
                raise PrivateStateMigrationError(
                    "Post-migration validation still reports pending changes."
                )
        except BaseException as error:
            try:
                _restore_backup(locked_plan, backup_path, manifest_path, manifest_existed)
            except BaseException as rollback_error:
                raise PrivateStateMigrationError(
                    f"Migration failed ({error}); rollback also failed ({rollback_error}). "
                    f"Backup preserved at {backup_path}."
                ) from rollback_error
            raise PrivateStateMigrationError(
                f"Migration failed and was rolled back: {error}. "
                f"Backup preserved at {backup_path}."
            ) from error
    return PrivateStateMigrationResult(
        dry_run=False,
        applied=True,
        plan=locked_plan,
        backup_path=backup_path,
    )


def format_private_state_plan(plan: PrivateStatePlan) -> str:
    if not plan.valid:
        return "\n".join(
            [
                "Private state validation failed.",
                f"Managed files checked: {plan.files_checked}",
                *(f"- {error}" for error in plan.errors),
            ]
        )
    lines = [
        "Private state validation passed.",
        f"Manifest: {plan.manifest_status}",
        f"Managed files checked: {plan.files_checked}",
    ]
    if not plan.migration_required:
        lines.append("Migration: not required")
        return "\n".join(lines)
    lines.append("Migration required:")
    if plan.manifest_status != "current":
        lines.append(f"- state manifest: {plan.manifest_status} -> current")
    lines.extend(
        f"- {action.relative_path}: v{action.from_version} -> v{action.to_version}"
        for action in plan.actions
    )
    return "\n".join(lines)


def format_private_state_migration(result: PrivateStateMigrationResult) -> str:
    plan_text = format_private_state_plan(result.plan)
    if result.dry_run:
        return "Private state migration dry run.\n" + plan_text
    if not result.applied:
        return "Private state is already current.\n" + plan_text
    return "\n".join(
        [
            "Private state migration completed.",
            f"Backup: {result.backup_path}",
            f"Migrated files: {len(result.plan.actions)}",
            "Manifest: current",
        ]
    )


def _manifest_status(root: Path | None) -> tuple[str, tuple[str, ...]]:
    path = private_state_manifest_path(root)
    if not path.exists():
        return "missing", ()
    try:
        data = load_json_object(path)
    except StateCorruptionError as error:
        return "invalid", (str(error),)
    schema_version = _non_negative_int(data.get("schema_version"))
    state_version = _non_negative_int(data.get("state_version"))
    schemas = data.get("schemas")
    errors = []
    if schema_version is None:
        errors.append(f"{path}: manifest schema_version must be a non-negative integer")
    elif schema_version > PRIVATE_STATE_MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"{path}: unsupported manifest schema version {schema_version}; "
            f"this Enoch supports {PRIVATE_STATE_MANIFEST_SCHEMA_VERSION}"
        )
    if state_version is None:
        errors.append(f"{path}: state_version must be a non-negative integer")
    elif state_version > PRIVATE_STATE_VERSION:
        errors.append(
            f"{path}: unsupported private-state version {state_version}; "
            f"this Enoch supports {PRIVATE_STATE_VERSION}"
        )
    if not isinstance(schemas, dict) or any(
        not isinstance(key, str)
        or _non_negative_int(value) is None
        for key, value in (schemas.items() if isinstance(schemas, dict) else ())
    ):
        errors.append(f"{path}: schemas must map path patterns to integer versions")
    if errors:
        return "invalid", tuple(errors)
    expected = _schema_registry()
    if (
        schema_version != PRIVATE_STATE_MANIFEST_SCHEMA_VERSION
        or state_version != PRIVATE_STATE_VERSION
        or schemas != expected
    ):
        for pattern, version in schemas.items():
            expected_version = expected.get(pattern)
            if expected_version is None:
                return (
                    "invalid",
                    (f"{path}: manifest contains unsupported state schema {pattern}",),
                )
            if version > expected_version:
                return (
                    "invalid",
                    (
                        f"{path}: {pattern} uses unsupported schema version {version}; "
                        f"this Enoch supports {expected_version}",
                    ),
                )
        return "outdated", ()
    return "current", ()


def _state_file_version(path: Path, schema: StateFileSchema) -> int:
    if not schema.embedded_version:
        try:
            _parse_simple_yaml(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise StateCorruptionError(path, str(error)) from error
        return schema.current_version
    data = load_json_object(path)
    raw_version = data.get("schema_version")
    if raw_version is None:
        return 0
    version = _non_negative_int(raw_version)
    if version is None:
        raise StateCorruptionError(path, "schema_version must be a non-negative integer")
    return version


def _normalized_payload(path: Path, schema: StateFileSchema) -> dict[str, Any]:
    if not schema.embedded_version:
        _parse_simple_yaml(path.read_text(encoding="utf-8"))
        return {}
    data = load_json_object(path)
    normalized = deepcopy(data)
    for key, default in schema.defaults:
        normalized.setdefault(key, deepcopy(default))
    for key, expected in schema.fields:
        value = normalized.get(key)
        if isinstance(value, bool) and bool not in expected:
            valid = False
        else:
            valid = isinstance(value, expected)
        if not valid:
            names = " or ".join(item.__name__ for item in expected)
            raise StateCorruptionError(path, f"expected {key} to be {names}")
    normalized["schema_version"] = schema.current_version
    if schema.pattern == "task_queue.json":
        return _normalize_task_queue(path, normalized)
    if schema.pattern == "backlog.json":
        return _normalize_backlog(path, normalized)
    if schema.pattern == "cron.json":
        return _normalize_cron(path, normalized)
    if schema.pattern == "evolve_candidates.json":
        return _normalize_evolve_candidates(path, normalized)
    if schema.pattern == "memory/long_term.json" and any(
        not isinstance(item, dict) for item in normalized["memories"]
    ):
        raise StateCorruptionError(path, "found an invalid memory entry")
    return normalized


def _normalize_task_queue(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    from enoch.tasks.queue import _job_to_dict, _parse_job

    parsed: dict[str, list[Any]] = {}
    for key in ("pending", "paused", "history"):
        jobs = [_parse_job(item) for item in data[key]]
        if any(job is None for job in jobs):
            raise StateCorruptionError(path, f"found an invalid task in {key}")
        parsed[key] = [job for job in jobs if job is not None]
    running = _parse_job(data.get("running"))
    if data.get("running") is not None and running is None:
        raise StateCorruptionError(path, "found an invalid running task")
    ids = [job.id for jobs in parsed.values() for job in jobs]
    if running is not None:
        ids.append(running.id)
    next_id = data["next_id"]
    if isinstance(next_id, bool):
        raise StateCorruptionError(path, "expected next_id to be int")
    return {
        "schema_version": data["schema_version"],
        "next_id": max(next_id, max(ids, default=0) + 1),
        "pending": [_job_to_dict(job) for job in parsed["pending"]],
        "paused": [_job_to_dict(job) for job in parsed["paused"]],
        "running": _job_to_dict(running) if running is not None else None,
        "history": [_job_to_dict(job) for job in parsed["history"]],
    }


def _normalize_backlog(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    from enoch.backlog import _item_to_dict, _parse_item

    parsed: dict[str, list[Any]] = {}
    for key in ("pending", "history"):
        items = [_parse_item(item) for item in data[key]]
        if any(item is None for item in items):
            raise StateCorruptionError(path, f"found an invalid backlog item in {key}")
        parsed[key] = [item for item in items if item is not None]
    ids = [item.id for items in parsed.values() for item in items]
    next_id = data["next_id"]
    if isinstance(next_id, bool):
        raise StateCorruptionError(path, "expected next_id to be int")
    return {
        "schema_version": data["schema_version"],
        "next_id": max(next_id, max(ids, default=0) + 1),
        "pending": [_item_to_dict(item) for item in parsed["pending"]],
        "history": [_item_to_dict(item) for item in parsed["history"]],
    }


def _normalize_cron(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    from enoch.cron import _job_to_dict, _parse_job

    parsed: dict[str, list[Any]] = {}
    for key in ("active", "history"):
        jobs = [_parse_job(item) for item in data[key]]
        if any(job is None for job in jobs):
            raise StateCorruptionError(path, f"found an invalid cron job in {key}")
        parsed[key] = [job for job in jobs if job is not None]
    ids = [job.id for jobs in parsed.values() for job in jobs]
    next_id = data["next_id"]
    if isinstance(next_id, bool):
        raise StateCorruptionError(path, "expected next_id to be int")
    return {
        "schema_version": data["schema_version"],
        "next_id": max(next_id, max(ids, default=0) + 1),
        "active": [_job_to_dict(job) for job in parsed["active"]],
        "history": [_job_to_dict(job) for job in parsed["history"]],
    }


def _normalize_evolve_candidates(
    path: Path,
    data: dict[str, Any],
) -> dict[str, Any]:
    from enoch.evolution.core import _candidate_from_json, _candidate_to_json

    candidates = [
        _candidate_from_json(item) if isinstance(item, dict) else None
        for item in data["candidates"]
    ]
    if any(candidate is None for candidate in candidates):
        raise StateCorruptionError(path, "found an invalid evolve candidate")
    return {
        "schema_version": data["schema_version"],
        "updated_at": str(data.get("updated_at") or _now()),
        "candidates": [
            _candidate_to_json(candidate)
            for candidate in candidates
            if candidate is not None
        ],
    }


def _schema_registry() -> dict[str, int]:
    return {schema.pattern: schema.current_version for schema in STATE_FILE_SCHEMAS}


def _current_manifest() -> dict[str, object]:
    return {
        "schema_version": PRIVATE_STATE_MANIFEST_SCHEMA_VERSION,
        "state_version": PRIVATE_STATE_VERSION,
        "updated_at": _now(),
        "schemas": _schema_registry(),
    }


def _schema_for_relative_path(relative_path: str) -> StateFileSchema | None:
    path = Path(relative_path)
    return next(
        (schema for schema in STATE_FILE_SCHEMAS if path.match(schema.pattern)),
        None,
    )


def _create_backup(plan: PrivateStatePlan, root: Path | None) -> Path:
    layout = storage_layout(root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = layout.private_path(
        Path(BACKUP_DIRECTORY) / f"state-v{PRIVATE_STATE_VERSION}-{stamp}-{uuid4().hex[:8]}"
    )
    backup.mkdir(parents=True, exist_ok=False)
    for action in plan.actions:
        source = layout.private_path(action.relative_path)
        target = backup / action.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = private_state_manifest_path(root)
    if manifest.exists():
        shutil.copy2(manifest, backup / MANIFEST_NAME)
    atomic_write(
        backup / "migration_plan.json",
        json.dumps(
            {
                "created_at": _now(),
                "manifest_status": plan.manifest_status,
                "actions": [action.__dict__ for action in plan.actions],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return backup


def _restore_backup(
    plan: PrivateStatePlan,
    backup: Path,
    manifest_path: Path,
    manifest_existed: bool,
) -> None:
    private_root = manifest_path.parent
    for action in plan.actions:
        source = backup / action.relative_path
        target = private_root / action.relative_path
        atomic_write(target, source.read_text(encoding="utf-8"))
    if manifest_existed:
        atomic_write(
            manifest_path,
            (backup / MANIFEST_NAME).read_text(encoding="utf-8"),
        )
    else:
        manifest_path.unlink(missing_ok=True)


def _require_daemon_stopped(root: Path | None) -> None:
    path = private_state_path("daemon_epoch.json", root)
    if not path.exists():
        return
    data = load_json_object(path)
    current = data.get("current")
    if not isinstance(current, dict):
        return
    pid = _positive_int(current.get("pid"))
    if pid is not None and _pid_is_alive(pid):
        raise PrivateStateMigrationError(
            f"Stop the running Enoch daemon (pid {pid}) before applying private-state migration."
        )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: object) -> int | None:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
