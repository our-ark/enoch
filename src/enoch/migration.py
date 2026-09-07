from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import tempfile
from typing import Any, Iterable
from uuid import uuid4
import zipfile

from enoch.config import read_config
from enoch.operations.update_tools import current_repository_revision
from enoch.paths import artifact_path, private_state_path, storage_layout
from enoch.private_state import (
    MANIFEST_NAME,
    PrivateStateError,
    assert_private_state_supported,
    plan_private_state,
    require_daemon_stopped as require_private_state_daemon_stopped,
)
from enoch.providers.registry import ProviderError, provider_name
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object
from enoch.vcs_tools import VcsError, ensure_clean_worktree


BUNDLE_SCHEMA_VERSION = 1
MIGRATION_STATE_SCHEMA_VERSION = 1
BUNDLE_MANIFEST = "migration.json"
SOURCE_MARKER = "migration_source.json"
TARGET_MARKER = "migration_target.json"
REPORT_DIRECTORY = "migrations"
MAX_BUNDLE_FILES = 100_000
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024

_PRIVATE_TOP_LEVEL_EXCLUSIONS = frozenset(
    {
        "artifacts",
        "backups",
        "dependencies",
        "config.yaml",
        "codex_sessions.json",
        "daemon_epoch.json",
        SOURCE_MARKER,
        TARGET_MARKER,
    }
)


class AgentMigrationError(RuntimeError):
    """Raised when a host-migration operation cannot complete safely."""


@dataclass(frozen=True)
class MigrationExportResult:
    migration_id: str
    bundle_path: Path
    bundle_sha256: str
    body_revision: str
    files: int
    bytes: int
    artifacts_included: bool


@dataclass(frozen=True)
class MigrationInspection:
    migration_id: str
    bundle_path: Path
    body_revision: str
    source_host: str
    source_authority_generation: int
    provider_bindings: dict[str, str]
    runtime_settings: dict[str, str]
    files: int
    bytes: int
    artifacts_included: bool
    manifest: dict[str, Any]


@dataclass(frozen=True)
class MigrationImportResult:
    migration_id: str
    bundle_path: Path
    applied: bool
    dry_run: bool
    files: int
    bytes: int
    target_marker: Path | None = None


@dataclass(frozen=True)
class MigrationVerification:
    migration_id: str
    passed: bool
    authority_active: bool
    report_path: Path
    checks: dict[str, bool]


@dataclass(frozen=True)
class MigrationActivation:
    migration_id: str
    authority_generation: int


@dataclass(frozen=True)
class _BundleFile:
    archive_path: str
    area: str
    relative_path: str
    source: Path
    size: int
    sha256: str


def export_migration_bundle(
    destination: Path,
    root: Path | None = None,
    *,
    include_artifacts: bool = False,
) -> MigrationExportResult:
    """Checkpoint portable agent state into a verified host-migration bundle."""

    resolved_root = Path(root or Path.cwd()).resolve()
    bundle_path = destination.expanduser().resolve()
    layout = storage_layout(resolved_root)
    _require_external_destination(bundle_path, layout)
    if bundle_path.exists():
        raise AgentMigrationError(f"Migration bundle already exists: {bundle_path}")
    _require_unfenced_source(resolved_root)
    _require_current_private_state(resolved_root)
    _require_clean_body(resolved_root)
    _require_quiescent_workflows(resolved_root)

    migration_id = uuid4().hex
    body_revision = _body_revision(resolved_root)
    source_generation = _authority_generation(resolved_root)
    providers = _provider_bindings(resolved_root)
    preparing_marker = {
        "schema_version": MIGRATION_STATE_SCHEMA_VERSION,
        "migration_id": migration_id,
        "status": "preparing",
        "created_at": _now(),
        "body_revision": body_revision,
        "source_host": platform.node() or "unknown",
        "source_authority_generation": source_generation,
    }
    marker_path = source_migration_marker_path(resolved_root)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{bundle_path.name}.",
        suffix=".tmp",
        dir=bundle_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        atomic_write(
            marker_path,
            json.dumps(preparing_marker, indent=2, sort_keys=True) + "\n",
        )
        _require_daemon_stopped(resolved_root)
        _require_current_private_state(resolved_root)
        _require_clean_body(resolved_root)
        if _body_revision(resolved_root) != body_revision:
            raise AgentMigrationError(
                "The software-body revision changed while migration export was preparing."
            )
        _require_quiescent_workflows(resolved_root)
        files = _portable_files(resolved_root, include_artifacts=include_artifacts)
        manifest = _bundle_manifest(
            migration_id=migration_id,
            root=resolved_root,
            body_revision=body_revision,
            source_generation=source_generation,
            provider_bindings=providers,
            files=files,
            include_artifacts=include_artifacts,
        )
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.writestr(
                BUNDLE_MANIFEST,
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )
            for item in files:
                archive.write(item.source, item.archive_path)
        _read_verified_bundle(temporary)
        os.chmod(temporary, 0o600)
        temporary.replace(bundle_path)
        _fsync_directory(bundle_path.parent)
        bundle_digest = _sha256_file(bundle_path)
        marker = {
            "schema_version": MIGRATION_STATE_SCHEMA_VERSION,
            "migration_id": migration_id,
            "status": "exported",
            "created_at": manifest["created_at"],
            "body_revision": body_revision,
            "source_host": manifest["source"]["host"],
            "source_authority_generation": source_generation,
            "bundle_sha256": bundle_digest,
        }
        try:
            atomic_write(marker_path, json.dumps(marker, indent=2, sort_keys=True) + "\n")
        except BaseException:
            bundle_path.unlink(missing_ok=True)
            raise
    except BaseException as error:
        temporary.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
        if isinstance(error, AgentMigrationError):
            raise
        raise AgentMigrationError(f"Could not export migration bundle: {error}") from error

    return MigrationExportResult(
        migration_id=migration_id,
        bundle_path=bundle_path,
        bundle_sha256=bundle_digest,
        body_revision=body_revision,
        files=len(files),
        bytes=sum(item.size for item in files),
        artifacts_included=include_artifacts,
    )


def inspect_migration_bundle(
    bundle: Path,
    root: Path | None = None,
    *,
    require_body_match: bool = False,
) -> MigrationInspection:
    bundle_path = bundle.expanduser().resolve()
    manifest, payloads = _read_verified_bundle(bundle_path)
    if require_body_match:
        resolved_root = Path(root or Path.cwd()).resolve()
        _require_clean_body(resolved_root)
        actual_revision = _body_revision(resolved_root)
        expected_revision = str(manifest["substrate"]["body_revision"])
        if actual_revision != expected_revision:
            raise AgentMigrationError(
                "Target body revision does not match the migration bundle: "
                f"expected {expected_revision}, found {actual_revision}."
            )
    del payloads
    source = manifest["source"]
    contents = manifest["contents"]
    return MigrationInspection(
        migration_id=str(manifest["migration_id"]),
        bundle_path=bundle_path,
        body_revision=str(manifest["substrate"]["body_revision"]),
        source_host=str(source["host"]),
        source_authority_generation=_non_negative_int(
            source.get("authority_generation"),
            "source.authority_generation",
        ),
        provider_bindings={
            str(key): str(value)
            for key, value in source.get("provider_bindings", {}).items()
        },
        runtime_settings={
            str(key): str(value)
            for key, value in source.get("runtime_settings", {}).items()
        },
        files=len(contents["files"]),
        bytes=sum(int(item["size"]) for item in contents["files"]),
        artifacts_included=bool(contents.get("artifacts_included", False)),
        manifest=manifest,
    )


def import_migration_bundle(
    bundle: Path,
    root: Path | None = None,
    *,
    dry_run: bool = False,
) -> MigrationImportResult:
    """Validate and transactionally install a bundle into a matching body checkout."""

    resolved_root = Path(root or Path.cwd()).resolve()
    bundle_path = bundle.expanduser().resolve()
    inspection = inspect_migration_bundle(
        bundle_path,
        resolved_root,
        require_body_match=True,
    )
    manifest, payloads = _read_verified_bundle(bundle_path)
    _require_daemon_stopped(resolved_root)
    assert_source_not_fenced(resolved_root)
    _require_import_target_available(resolved_root, manifest)
    if dry_run:
        return MigrationImportResult(
            migration_id=inspection.migration_id,
            bundle_path=bundle_path,
            applied=False,
            dry_run=True,
            files=inspection.files,
            bytes=inspection.bytes,
        )

    layout = storage_layout(resolved_root)
    written: list[Path] = []
    target_marker = target_migration_marker_path(resolved_root)
    entries = list(manifest["contents"]["files"])
    entries.sort(key=lambda item: str(item["relative_path"]) == MANIFEST_NAME)
    try:
        marker = {
            "schema_version": MIGRATION_STATE_SCHEMA_VERSION,
            "migration_id": inspection.migration_id,
            "status": "importing",
            "imported_at": "",
            "source_host": inspection.source_host,
            "target_host": platform.node() or "unknown",
            "source_authority_generation": inspection.source_authority_generation,
            "body_revision": inspection.body_revision,
            "bundle_sha256": _sha256_file(bundle_path),
            "manifest": manifest,
        }
        atomic_write(
            target_marker,
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
        )
        written.append(target_marker)
        _require_daemon_stopped(resolved_root)
        for entry in entries:
            archive_path = str(entry["archive_path"])
            target = _entry_target(layout, entry)
            _atomic_write_bytes(target, payloads[archive_path])
            os.chmod(target, 0o600)
            written.append(target)
        _require_current_private_state(resolved_root)
        marker["status"] = "imported"
        marker["imported_at"] = _now()
        atomic_write(
            target_marker,
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
        )
        _require_current_private_state(resolved_root)
    except BaseException as error:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        _remove_empty_parents(written, layout)
        if isinstance(error, AgentMigrationError):
            raise
        raise AgentMigrationError(
            f"Migration import failed; newly written target files were removed: {error}"
        ) from error

    return MigrationImportResult(
        migration_id=inspection.migration_id,
        bundle_path=bundle_path,
        applied=True,
        dry_run=False,
        files=inspection.files,
        bytes=inspection.bytes,
        target_marker=target_marker,
    )


def verify_imported_migration(root: Path | None = None) -> MigrationVerification:
    resolved_root = Path(root or Path.cwd()).resolve()
    marker_path = target_migration_marker_path(resolved_root)
    marker = _load_marker(marker_path, "target migration")
    manifest = marker.get("manifest")
    if not isinstance(manifest, dict):
        raise AgentMigrationError(f"Target migration marker is missing its manifest: {marker_path}")
    checks: dict[str, bool] = {}
    try:
        private_state_plan = plan_private_state(resolved_root)
        checks["private_state_supported"] = (
            private_state_plan.valid and not private_state_plan.migration_required
        )
    except (OSError, StateCorruptionError, ValueError):
        checks["private_state_supported"] = False
    try:
        _require_clean_body(resolved_root)
        checks["body_revision"] = (
            _body_revision(resolved_root)
            == str(manifest["substrate"]["body_revision"])
        )
    except (AgentMigrationError, KeyError, TypeError):
        checks["body_revision"] = False

    layout = storage_layout(resolved_root)
    files_match = True
    for entry in manifest.get("contents", {}).get("files", []):
        try:
            target = _entry_target(layout, entry)
            files_match = files_match and target.is_file() and (
                _sha256_file(target) == str(entry["sha256"])
            )
        except (AgentMigrationError, KeyError, OSError, TypeError):
            files_match = False
    checks["portable_file_hashes"] = files_match

    source_generation = _non_negative_int(
        marker.get("source_authority_generation"),
        "source_authority_generation",
    )
    target_generation = _authority_generation(resolved_root)
    authority_active = target_generation > source_generation
    checks["authority_advanced"] = authority_active
    passed = all(checks.values())
    report = {
        "schema_version": MIGRATION_STATE_SCHEMA_VERSION,
        "migration_id": str(marker.get("migration_id") or ""),
        "verified_at": _now(),
        "source_host": str(marker.get("source_host") or ""),
        "target_host": platform.node() or "unknown",
        "source_provider_bindings": manifest.get("source", {}).get(
            "provider_bindings", {}
        ),
        "target_provider_bindings": _provider_bindings(resolved_root),
        "source_runtime_settings": manifest.get("source", {}).get(
            "runtime_settings", {}
        ),
        "target_runtime_settings": _runtime_settings(resolved_root),
        "source_authority_generation": source_generation,
        "target_authority_generation": target_generation,
        "authority_active": authority_active,
        "checks": checks,
        "passed": passed,
        "substrate": manifest.get("substrate", {}),
    }
    report_path = artifact_path(
        Path(REPORT_DIRECTORY) / str(marker["migration_id"]) / "migration-report.json",
        resolved_root,
    )
    atomic_write(report_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    if passed:
        with file_transaction(marker_path):
            verified_marker = _load_marker(marker_path, "target migration")
            verified_marker["status"] = "verified"
            verified_marker["verified_at"] = report["verified_at"]
            verified_marker["verification_report"] = str(report_path)
            atomic_write(
                marker_path,
                json.dumps(verified_marker, indent=2, sort_keys=True) + "\n",
            )
    return MigrationVerification(
        migration_id=str(marker["migration_id"]),
        passed=passed,
        authority_active=authority_active,
        report_path=report_path,
        checks=checks,
    )


def activate_imported_migration(root: Path | None = None) -> MigrationActivation:
    """Promote an imported checkpoint before the target service is resumed."""

    resolved_root = Path(root or Path.cwd()).resolve()
    marker = _load_marker(target_migration_marker_path(resolved_root), "target migration")
    status = str(marker.get("status") or "")
    if status in {"activated", "verified"}:
        generation = _non_negative_int(
            marker.get("target_authority_generation"),
            "target_authority_generation",
        )
        return MigrationActivation(str(marker["migration_id"]), generation)
    if status != "imported":
        raise AgentMigrationError(
            f"Migration {marker.get('migration_id', 'unknown')} is not ready for activation "
            f"(status={status or 'unknown'})."
        )
    _require_checkpoint_integrity(resolved_root, marker)
    from enoch.app.epoch import begin_daemon_epoch

    epoch = begin_daemon_epoch(
        resolved_root,
        provider="migration",
        migration_activation=True,
    )
    return MigrationActivation(str(marker["migration_id"]), epoch.generation)


def cancel_exported_migration(
    migration_id: str,
    root: Path | None = None,
) -> Path:
    """Release a source fence after the operator confirms no target is active."""

    resolved_root = Path(root or Path.cwd()).resolve()
    marker_path = source_migration_marker_path(resolved_root)
    with file_transaction(marker_path):
        marker = _load_marker(marker_path, "source migration")
        actual = str(marker.get("migration_id") or "")
        if migration_id.strip() != actual:
            raise AgentMigrationError(
                f"Migration id does not match the source fence: expected {actual}."
            )
        marker_path.unlink(missing_ok=True)
    return marker_path


def migration_status(root: Path | None = None) -> str:
    resolved_root = Path(root or Path.cwd()).resolve()
    lines = ["Host migration status:"]
    found = False
    for role, path in (
        ("source", source_migration_marker_path(resolved_root)),
        ("target", target_migration_marker_path(resolved_root)),
    ):
        if not path.exists():
            continue
        found = True
        marker = _load_marker(path, f"{role} migration")
        lines.extend(
            [
                f"- role: {role}",
                f"  migration: {marker.get('migration_id', 'unknown')}",
                f"  status: {marker.get('status', 'unknown')}",
                f"  source host: {marker.get('source_host', 'unknown')}",
            ]
        )
        if role == "target":
            lines.append(f"  target host: {marker.get('target_host', 'unknown')}")
    if not found:
        lines.append("- no migration marker")
    return "\n".join(lines)


def source_migration_marker_path(root: Path | None = None) -> Path:
    return private_state_path(SOURCE_MARKER, root)


def target_migration_marker_path(root: Path | None = None) -> Path:
    return private_state_path(TARGET_MARKER, root)


def assert_source_not_fenced(root: Path | None = None) -> None:
    path = source_migration_marker_path(root)
    if not path.exists():
        return
    marker = _load_marker(path, "source migration")
    migration_id = str(marker.get("migration_id") or "unknown")
    raise AgentMigrationError(
        "This source instance is fenced by exported migration "
        f"{migration_id}. Start the imported target, or cancel the export only "
        "after confirming that no target continuation is active."
    )


def assert_runtime_start_allowed(root: Path | None = None) -> None:
    assert_source_not_fenced(root)
    path = target_migration_marker_path(root)
    if not path.exists():
        return
    marker = _load_marker(path, "target migration")
    if marker.get("status") not in {"activated", "verified"}:
        raise AgentMigrationError(
            f"Imported migration {marker.get('migration_id', 'unknown')} must be "
            "activated and verified before the agent service starts."
        )


def migration_authority_floor(root: Path | None = None) -> int:
    path = target_migration_marker_path(root)
    if not path.exists():
        return 0
    marker = _load_marker(path, "target migration")
    return _non_negative_int(
        marker.get("source_authority_generation"),
        "source_authority_generation",
    )


def record_target_activation(
    generation: int,
    root: Path | None = None,
) -> None:
    path = target_migration_marker_path(root)
    if not path.exists():
        return
    with file_transaction(path):
        marker = _load_marker(path, "target migration")
        if marker.get("status") != "verified":
            marker["status"] = "activated"
        marker["activated_at"] = _now()
        marker["target_authority_generation"] = generation
        atomic_write(path, json.dumps(marker, indent=2, sort_keys=True) + "\n")


def format_export_result(result: MigrationExportResult) -> str:
    artifact_note = "included" if result.artifacts_included else "not included"
    return "\n".join(
        [
            "Host migration checkpoint exported and source fenced.",
            f"Migration: {result.migration_id}",
            f"Bundle: {result.bundle_path}",
            f"SHA-256: {result.bundle_sha256}",
            f"Body revision: {result.body_revision}",
            f"Portable files: {result.files} ({result.bytes} bytes)",
            f"Artifacts: {artifact_note}",
            "Next: transfer the bundle securely and run migration import --dry-run on the target.",
        ]
    )


def format_inspection(result: MigrationInspection) -> str:
    providers = ", ".join(
        f"{kind}={name}" for kind, name in sorted(result.provider_bindings.items())
    ) or "none recorded"
    runtime_settings = ", ".join(
        f"{key}={value}" for key, value in sorted(result.runtime_settings.items())
    ) or "defaults"
    return "\n".join(
        [
            "Migration bundle validation passed.",
            f"Migration: {result.migration_id}",
            f"Source host: {result.source_host}",
            f"Body revision: {result.body_revision}",
            f"Source authority generation: {result.source_authority_generation}",
            f"Source providers: {providers}",
            f"Source runtime settings: {runtime_settings}",
            f"Portable files: {result.files} ({result.bytes} bytes)",
            f"Artifacts included: {'yes' if result.artifacts_included else 'no'}",
            "Credentials, host process state, and native runtime session mappings are not included.",
        ]
    )


def format_import_result(result: MigrationImportResult) -> str:
    if result.dry_run:
        return "\n".join(
            [
                "Migration import dry run passed; target was not changed.",
                f"Migration: {result.migration_id}",
                f"Portable files: {result.files} ({result.bytes} bytes)",
            ]
        )
    return "\n".join(
        [
            "Migration bundle imported; continuation authority is not active yet.",
            f"Migration: {result.migration_id}",
            f"Portable files: {result.files} ({result.bytes} bytes)",
            f"Target marker: {result.target_marker}",
            "Next: configure target credentials, run doctor, activate and verify "
            "the migration, then start the daemon.",
        ]
    )


def format_verification(result: MigrationVerification) -> str:
    lines = [
        "Migration verification passed."
        if result.passed
        else "Migration verification needs attention.",
        f"Migration: {result.migration_id}",
    ]
    lines.extend(
        f"- {name}: {'ok' if passed else 'failed'}"
        for name, passed in result.checks.items()
    )
    lines.append(f"Report: {result.report_path}")
    return "\n".join(lines)


def format_activation(result: MigrationActivation) -> str:
    return "\n".join(
        [
            "Imported migration activated on this target.",
            f"Migration: {result.migration_id}",
            f"Authority generation: {result.authority_generation}",
            "Next: run migration verify, then start the daemon.",
        ]
    )


def _require_unfenced_source(root: Path) -> None:
    assert_source_not_fenced(root)
    target_marker = target_migration_marker_path(root)
    if target_marker.exists() and _load_marker(
        target_marker, "target migration"
    ).get("status") != "verified":
        raise AgentMigrationError(
            "This checkout is already an imported migration target and cannot export "
            "another checkpoint until its migration has been verified."
        )


def _require_external_destination(destination: Path, layout: Any) -> None:
    for area in ("software-body", "private-state", "artifacts"):
        if layout.contains(area, destination):
            raise AgentMigrationError(
                "Write the migration bundle outside the software body and private storage."
            )


def _require_current_private_state(root: Path) -> None:
    try:
        plan = assert_private_state_supported(root)
    except Exception as error:
        raise AgentMigrationError(str(error)) from error
    if plan.migration_required:
        raise AgentMigrationError(
            "Private state must be current before host migration. Run "
            "`bin/enoch state migrate --dry-run`, then `bin/enoch state migrate`."
        )


def _require_daemon_stopped(root: Path) -> None:
    try:
        require_private_state_daemon_stopped(root)
    except PrivateStateError as error:
        raise AgentMigrationError(str(error)) from error


def _require_clean_body(root: Path) -> None:
    try:
        ensure_clean_worktree(root)
    except VcsError as error:
        raise AgentMigrationError(
            "The software body must be a clean, versioned revision before migration: "
            f"{error}"
        ) from error


def _body_revision(root: Path) -> str:
    try:
        revision = current_repository_revision(root).strip()
    except VcsError as error:
        raise AgentMigrationError(
            f"Could not resolve the software-body revision: {error}"
        ) from error
    if not revision:
        raise AgentMigrationError("Could not resolve the software-body revision.")
    return revision


def _require_quiescent_workflows(root: Path) -> None:
    path = private_state_path("task_queue.json", root)
    if not path.exists():
        return
    try:
        queue = load_json_object(path)
    except StateCorruptionError as error:
        raise AgentMigrationError(str(error)) from error
    running = queue.get("running")
    if isinstance(running, dict):
        task_id = running.get("id", "unknown")
        raise AgentMigrationError(
            f"Task #{task_id} is still running. Quiesce or pause active work before export."
        )
    for section in ("pending", "paused"):
        jobs = queue.get(section, [])
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            workspace = str(job.get("workspace_path") or "").strip()
            if workspace:
                raise AgentMigrationError(
                    f"Task #{job.get('id', 'unknown')} references host-local workspace "
                    f"{workspace}. Capture or reconcile it before host migration."
                )


def _portable_files(root: Path, *, include_artifacts: bool) -> list[_BundleFile]:
    layout = storage_layout(root)
    files = list(
        _walk_files(
            layout.private_state,
            area="private-state",
            archive_root="state",
            excluded=_private_state_excluded,
        )
    )
    if include_artifacts and layout.artifacts.exists():
        files.extend(
            _walk_files(
                layout.artifacts,
                area="artifacts",
                archive_root="artifacts",
                excluded=lambda _relative: False,
            )
        )
    files.sort(key=lambda item: item.archive_path)
    if len(files) > MAX_BUNDLE_FILES:
        raise AgentMigrationError(
            f"Migration contains {len(files)} files; maximum is {MAX_BUNDLE_FILES}."
        )
    total = sum(item.size for item in files)
    if total > MAX_BUNDLE_BYTES:
        raise AgentMigrationError(
            f"Migration contains {total} bytes; maximum is {MAX_BUNDLE_BYTES}."
        )
    return files


def _walk_files(
    base: Path,
    *,
    area: str,
    archive_root: str,
    excluded: Any,
) -> Iterable[_BundleFile]:
    if not base.exists():
        return ()
    collected: list[_BundleFile] = []
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if excluded(relative):
            continue
        if path.is_symlink():
            raise AgentMigrationError(f"Migration state may not contain symlinks: {path}")
        if not path.is_file():
            continue
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise AgentMigrationError(f"Migration state must be a regular file: {path}")
        archive_path = f"{archive_root}/{relative.as_posix()}"
        collected.append(
            _BundleFile(
                archive_path=archive_path,
                area=area,
                relative_path=relative.as_posix(),
                source=path,
                size=path.stat().st_size,
                sha256=_sha256_file(path),
            )
        )
    return tuple(collected)


def _private_state_excluded(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return True
    if parts[0] in _PRIVATE_TOP_LEVEL_EXCLUSIONS:
        return True
    if relative.name.endswith((".lock", ".tmp")):
        return True
    return len(parts) >= 3 and parts[0] == "runtime" and relative.name == "sessions.json"


def _bundle_manifest(
    *,
    migration_id: str,
    root: Path,
    body_revision: str,
    source_generation: int,
    provider_bindings: dict[str, str],
    files: list[_BundleFile],
    include_artifacts: bool,
) -> dict[str, Any]:
    identity = next(
        (item.sha256 for item in files if item.archive_path == "state/self.json"),
        "",
    )
    memory_entries = [
        item for item in files if item.archive_path.startswith("state/memory/")
    ]
    task_snapshot = _task_snapshot(root)
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "migration_id": migration_id,
        "created_at": _now(),
        "source": {
            "host": platform.node() or "unknown",
            "platform": platform.platform(),
            "authority_generation": source_generation,
            "provider_bindings": provider_bindings,
            "runtime_settings": _runtime_settings(root, provider_bindings),
        },
        "substrate": {
            "identity_sha256": identity,
            "memory_sha256": _aggregate_digest(memory_entries),
            "body_revision": body_revision,
            "tasks": task_snapshot,
        },
        "contents": {
            "artifacts_included": include_artifacts,
            "files": [
                {
                    "archive_path": item.archive_path,
                    "area": item.area,
                    "relative_path": item.relative_path,
                    "size": item.size,
                    "sha256": item.sha256,
                }
                for item in files
            ],
            "excluded": [
                "credentials and config.yaml",
                "daemon process state",
                "downloaded dependencies and backups",
                "provider-native runtime session mappings",
                *([] if include_artifacts else ["artifact storage"]),
            ],
        },
    }


def _task_snapshot(root: Path) -> dict[str, list[int]]:
    path = private_state_path("task_queue.json", root)
    if not path.exists():
        return {key: [] for key in ("pending", "paused", "running", "history")}
    queue = load_json_object(path)
    result: dict[str, list[int]] = {}
    for section in ("pending", "paused", "history"):
        raw = queue.get(section, [])
        result[section] = [
            int(item["id"])
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ] if isinstance(raw, list) else []
    running = queue.get("running")
    result["running"] = (
        [int(running["id"])]
        if isinstance(running, dict) and isinstance(running.get("id"), int)
        else []
    )
    return result


def _aggregate_digest(files: list[_BundleFile]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda candidate: candidate.relative_path):
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _provider_bindings(root: Path) -> dict[str, str]:
    config = _migration_config(root)
    bindings: dict[str, str] = {}
    for kind in ("chat", "runtime", "vcs", "forge", "service"):
        try:
            bindings[kind] = provider_name(kind, root)
        except (ProviderError, OSError, ValueError):
            configured = config.get("providers", {}).get(kind, "")
            bindings[kind] = configured or "unavailable"
    return bindings


def _runtime_settings(
    root: Path,
    bindings: dict[str, str] | None = None,
) -> dict[str, str]:
    selected = (bindings or _provider_bindings(root)).get("runtime", "")
    settings = _migration_config(root).get(selected, {}) if selected else {}
    return {
        key: str(settings[key])
        for key in ("model", "reasoning_effort")
        if str(settings.get(key) or "").strip()
    }


def _migration_config(root: Path) -> dict[str, dict[str, str]]:
    try:
        return read_config(root)
    except (OSError, ValueError) as error:
        raise AgentMigrationError(f"Private provider configuration is invalid: {error}") from error


def _authority_generation(root: Path) -> int:
    path = private_state_path("daemon_epoch.json", root)
    if not path.exists():
        return 0
    data = load_json_object(path)
    current = data.get("current")
    if not isinstance(current, dict):
        return 0
    value = current.get("generation")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_verified_bundle(bundle: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not bundle.is_file():
        raise AgentMigrationError(f"Migration bundle does not exist: {bundle}")
    try:
        with zipfile.ZipFile(bundle, mode="r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_BUNDLE_FILES + 1:
                raise AgentMigrationError("Migration bundle contains too many files.")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise AgentMigrationError("Migration bundle contains duplicate paths.")
            for info in infos:
                _validate_archive_member(info)
            if BUNDLE_MANIFEST not in names:
                raise AgentMigrationError("Migration bundle is missing migration.json.")
            total = sum(info.file_size for info in infos)
            if total > MAX_BUNDLE_BYTES:
                raise AgentMigrationError("Migration bundle expands beyond the size limit.")
            try:
                manifest = json.loads(archive.read(BUNDLE_MANIFEST).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AgentMigrationError(f"Migration manifest is unreadable: {error}") from error
            _validate_manifest(manifest)
            expected = {
                str(item["archive_path"]): item
                for item in manifest["contents"]["files"]
            }
            extras = set(names) - {BUNDLE_MANIFEST, *expected}
            missing = set(expected) - set(names)
            if extras or missing:
                raise AgentMigrationError(
                    "Migration bundle contents do not match the manifest "
                    f"(extra={sorted(extras)}, missing={sorted(missing)})."
                )
            payloads: dict[str, bytes] = {}
            for name, entry in expected.items():
                payload = archive.read(name)
                if len(payload) != int(entry["size"]):
                    raise AgentMigrationError(f"Migration file size mismatch: {name}")
                if hashlib.sha256(payload).hexdigest() != str(entry["sha256"]):
                    raise AgentMigrationError(f"Migration file checksum mismatch: {name}")
                payloads[name] = payload
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, AgentMigrationError):
            raise
        raise AgentMigrationError(f"Could not read migration bundle: {error}") from error
    return manifest, payloads


def _validate_archive_member(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if info.flag_bits & 0x1:
        raise AgentMigrationError("Encrypted zip members are not supported.")
    if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
        raise AgentMigrationError(f"Unsafe path in migration bundle: {info.filename}")
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise AgentMigrationError(f"Symlink in migration bundle: {info.filename}")


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise AgentMigrationError("Migration manifest must be a JSON object.")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise AgentMigrationError(
            f"Unsupported migration bundle schema: {manifest.get('schema_version')}."
        )
    migration_id = str(manifest.get("migration_id") or "").strip()
    if len(migration_id) != 32 or any(
        character not in "0123456789abcdef" for character in migration_id
    ):
        raise AgentMigrationError("Migration manifest has an invalid migration id.")
    source = manifest.get("source")
    substrate = manifest.get("substrate")
    contents = manifest.get("contents")
    if (
        not isinstance(source, dict)
        or not isinstance(substrate, dict)
        or not isinstance(contents, dict)
    ):
        raise AgentMigrationError("Migration manifest is missing source, substrate, or contents.")
    provider_bindings = source.get("provider_bindings")
    if not isinstance(provider_bindings, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in provider_bindings.items()
    ):
        raise AgentMigrationError("Migration source provider bindings must be strings.")
    runtime_settings = source.get("runtime_settings", {})
    if not isinstance(runtime_settings, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in runtime_settings.items()
    ):
        raise AgentMigrationError("Migration source runtime settings must be strings.")
    revision = str(substrate.get("body_revision") or "").strip()
    if not revision:
        raise AgentMigrationError("Migration manifest is missing the body revision.")
    files = contents.get("files")
    if not isinstance(files, list):
        raise AgentMigrationError("Migration manifest files must be a list.")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise AgentMigrationError("Migration manifest contains an invalid file entry.")
        archive_path = str(entry.get("archive_path") or "")
        area = str(entry.get("area") or "")
        relative = str(entry.get("relative_path") or "")
        if archive_path in seen:
            raise AgentMigrationError(f"Duplicate migration manifest path: {archive_path}")
        seen.add(archive_path)
        expected_prefix = {
            "private-state": "state/",
            "artifacts": "artifacts/",
        }.get(area, "")
        if not expected_prefix or archive_path != expected_prefix + relative:
            raise AgentMigrationError(f"Invalid migration file mapping: {archive_path}")
        _safe_relative_path(relative)
        size = entry.get("size")
        checksum = str(entry.get("sha256") or "")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AgentMigrationError(f"Invalid migration file size: {archive_path}")
        if len(checksum) != 64 or any(
            character not in "0123456789abcdef" for character in checksum
        ):
            raise AgentMigrationError(f"Invalid migration checksum: {archive_path}")


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise AgentMigrationError(f"Unsafe relative migration path: {value}")
    return path


def _entry_target(layout: Any, entry: dict[str, Any]) -> Path:
    relative = _safe_relative_path(str(entry["relative_path"]))
    if entry["area"] == "private-state":
        return layout.private_path(Path(*relative.parts))
    if entry["area"] == "artifacts":
        return layout.artifact_path(Path(*relative.parts))
    raise AgentMigrationError(f"Unknown migration storage area: {entry['area']}")


def _require_import_target_available(root: Path, manifest: dict[str, Any]) -> None:
    target_marker = target_migration_marker_path(root)
    if target_marker.exists():
        marker = _load_marker(target_marker, "target migration")
        if marker.get("migration_id") == manifest.get("migration_id"):
            raise AgentMigrationError(
                f"Migration {manifest['migration_id']} is already imported in this target."
            )
        raise AgentMigrationError("This target already contains another imported migration.")
    layout = storage_layout(root)
    conflicts = [
        _entry_target(layout, entry)
        for entry in manifest["contents"]["files"]
        if _entry_target(layout, entry).exists()
    ]
    if conflicts:
        rendered = ", ".join(str(path) for path in conflicts[:5])
        suffix = " ..." if len(conflicts) > 5 else ""
        raise AgentMigrationError(
            "Target already contains continuity-bearing files; import will not overwrite "
            f"another instance: {rendered}{suffix}"
        )


def _require_checkpoint_integrity(root: Path, marker: dict[str, Any]) -> None:
    manifest = marker.get("manifest")
    if not isinstance(manifest, dict):
        raise AgentMigrationError("Imported migration marker is missing its manifest.")
    _require_clean_body(root)
    if _body_revision(root) != str(manifest.get("substrate", {}).get("body_revision") or ""):
        raise AgentMigrationError("Target body revision changed after migration import.")
    layout = storage_layout(root)
    for entry in manifest.get("contents", {}).get("files", []):
        target = _entry_target(layout, entry)
        if not target.is_file() or _sha256_file(target) != str(entry.get("sha256") or ""):
            raise AgentMigrationError(
                f"Imported checkpoint changed before activation: {target}"
            )
    _require_current_private_state(root)


def _load_marker(path: Path, label: str) -> dict[str, Any]:
    try:
        marker = load_json_object(path)
    except StateCorruptionError as error:
        raise AgentMigrationError(str(error)) from error
    if marker.get("schema_version") != MIGRATION_STATE_SCHEMA_VERSION:
        raise AgentMigrationError(f"Unsupported {label} marker at {path}.")
    migration_id = str(marker.get("migration_id") or "").strip()
    if not migration_id:
        raise AgentMigrationError(f"Invalid {label} marker at {path}.")
    return marker


def _remove_empty_parents(paths: list[Path], layout: Any) -> None:
    roots = {layout.private_state, layout.artifacts}
    for path in paths:
        parent = path.parent
        while parent not in roots and any(
            _is_relative_to(parent, root) for root in roots
        ):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentMigrationError(f"{label} must be a non-negative integer.")
    return value


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
