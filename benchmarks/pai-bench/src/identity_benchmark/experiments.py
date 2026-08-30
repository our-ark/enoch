from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any

from identity_benchmark.adapters import CommandInstance
from identity_benchmark.contracts import (
    BenchmarkProfile,
    BenchmarkProfileError,
    BenchmarkReport,
    JsonValue,
    Probe,
    load_benchmark_profile,
    parse_benchmark_report,
)
from identity_benchmark.runner import (
    ReportIntegrityError,
    run_benchmark,
    validate_report_integrity,
)
from identity_benchmark.evaluators import CommandEvaluator
from identity_benchmark.probe_suites import (
    ProbeSuiteError,
    compile_benchmark_profile,
    load_identity_profile,
    load_probe_bindings,
    load_probe_suite,
)
from identity_benchmark.scoring import DeterministicScorer, normalize_text


EXPERIMENT_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")


class ExperimentError(ValueError):
    """Raised when an experiment matrix is malformed or cannot be saved."""


@dataclass(frozen=True)
class EvaluatorSpec:
    evaluator_id: str
    harness: str
    command: tuple[str, ...]
    model: str
    reasoning_effort: str
    rubric_version: str
    timeout_seconds: float = 600.0


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    profile_paths: tuple[Path, ...]
    body_root: Path
    instance_command: tuple[str, ...]
    models: tuple[str, ...]
    reasoning_efforts: tuple[str, ...]
    identity_modes: tuple[str, ...]
    evaluator: EvaluatorSpec | None = None
    counterfactual_pairs: tuple[tuple[str, str], ...] = ()
    repetitions: int = 1
    timeout_seconds: float = 600.0
    population_path: Path | None = None
    probe_suite_path: Path | None = None
    probe_binding_paths: tuple[tuple[str, Path], ...] = ()

    @property
    def profile_path(self) -> Path:
        """Return the sole/first profile for v1 caller compatibility."""
        return self.profile_paths[0]


@dataclass(frozen=True)
class ExperimentRun:
    run_id: str
    profile_id: str
    model: str
    reasoning_effort: str
    identity_mode: str
    repetition: int
    report: BenchmarkReport
    experiment_id: str = ""
    fingerprint: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "profile_id": self.profile_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "identity_mode": self.identity_mode,
            "repetition": self.repetition,
            "report": self.report.to_dict(),
        }


@dataclass(frozen=True)
class PlannedRun:
    run_id: str
    profile_path: Path
    profile: BenchmarkProfile
    model: str
    reasoning_effort: str
    identity_mode: str
    repetition: int
    fingerprint: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "profile_id": self.profile.profile_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "identity_mode": self.identity_mode,
            "repetition": self.repetition,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ExperimentPlan:
    experiment_id: str
    campaign_fingerprint: str
    runs: tuple[PlannedRun, ...]
    selected_runs: tuple[PlannedRun, ...]
    batch_size: int
    batch_index: int
    total_batches: int

    @property
    def total_runs(self) -> int:
        return len(self.runs)

    def to_dict(self, *, include_selection: bool = True) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "campaign_fingerprint": self.campaign_fingerprint,
            "total_runs": self.total_runs,
            "runs": [run.to_dict() for run in self.runs],
        }
        if include_selection:
            value.update(
                {
                    "batch_size": self.batch_size,
                    "batch_index": self.batch_index,
                    "total_batches": self.total_batches,
                    "selected_run_ids": [run.run_id for run in self.selected_runs],
                }
            )
        return value


@dataclass(frozen=True)
class ExperimentReport:
    experiment_id: str
    profile_ids: tuple[str, ...]
    started_at: str
    finished_at: str
    runs: tuple[ExperimentRun, ...]
    aggregates: tuple[dict[str, JsonValue], ...]
    identity_gains: tuple[dict[str, JsonValue], ...]
    counterfactual_metrics: tuple[dict[str, JsonValue], ...] = ()
    schema_version: int = EXPERIMENT_SCHEMA_VERSION
    population_aggregates: tuple[dict[str, JsonValue], ...] = ()
    total_runs: int = 0
    completed_runs: int = 0
    selected_run_ids: tuple[str, ...] = ()
    batch_size: int = 0
    batch_index: int = 1
    total_batches: int = 1

    @property
    def profile_id(self) -> str:
        """Return the sole profile ID, or a stable multi-profile label."""
        return self.profile_ids[0] if len(self.profile_ids) == 1 else "multiple"

    @property
    def errors(self) -> int:
        return sum(run.report.errors for run in self.runs)

    @property
    def is_complete(self) -> bool:
        total = self.total_runs or len(self.runs)
        completed = self.completed_runs or sum(
            run.report.errors == 0 for run in self.runs
        )
        return total > 0 and completed == total

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "profile_id": self.profile_id,
            "profile_ids": list(self.profile_ids),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "errors": self.errors,
            "status": "complete" if self.is_complete else "partial",
            "progress": {
                "total_runs": self.total_runs or len(self.runs),
                "attempted_runs": len(self.runs),
                "completed_runs": self.completed_runs
                or sum(run.report.errors == 0 for run in self.runs),
                "batch_size": self.batch_size or (self.total_runs or len(self.runs)),
                "batch_index": self.batch_index,
                "total_batches": self.total_batches,
                "selected_run_ids": list(self.selected_run_ids),
            },
            "evaluator_ids": sorted(
                {run.report.evaluator_id for run in self.runs}
            ),
            "runs": [run.to_dict() for run in self.runs],
            "aggregates": list(self.aggregates),
            "identity_gains": list(self.identity_gains),
            "counterfactual_metrics": list(self.counterfactual_metrics),
            "population_aggregates": list(self.population_aggregates),
        }


def load_experiment_spec(path: Path) -> ExperimentSpec:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"Could not load experiment manifest: {error}") from error
    return parse_experiment_spec(value, base=path.resolve().parent)


def parse_experiment_spec(value: object, *, base: Path) -> ExperimentSpec:
    root = _mapping(value, "experiment manifest")
    _keys(
        root,
        "experiment manifest",
        required={
            "schema_version",
            "experiment_id",
            "body_root",
            "instance_command",
            "models",
            "reasoning_efforts",
            "identity_modes",
        },
        optional={
            "$schema",
            "profile",
            "profiles",
            "counterfactual_pairs",
            "repetitions",
            "timeout_seconds",
            "evaluator",
            "population",
            "probe_suite",
            "probe_bindings",
        },
    )
    if root["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
        raise ExperimentError(
            f"schema_version must be {EXPERIMENT_SCHEMA_VERSION}; received {root['schema_version']!r}."
        )
    if "$schema" in root:
        _text(root["$schema"], "$schema")
    profile_paths = _profile_paths(root, base)
    body_root = _path(root["body_root"], "body_root", base)
    command = tuple(
        _text(item, "instance_command[]")
        for item in _nonempty_list(root["instance_command"], "instance_command")
    )
    models = _unique_texts(root["models"], "models")
    reasoning_efforts = _unique_texts(root["reasoning_efforts"], "reasoning_efforts")
    identity_modes = _unique_texts(root["identity_modes"], "identity_modes")
    counterfactual_pairs = _counterfactual_pairs(
        root.get("counterfactual_pairs", [])
    )
    repetitions = _positive_int(root.get("repetitions", 1), "repetitions")
    timeout_seconds = _positive_number(
        root.get("timeout_seconds", 600.0), "timeout_seconds"
    )
    evaluator = _evaluator_spec(root.get("evaluator"))
    population_path = (
        _path(root["population"], "population", base)
        if "population" in root
        else None
    )
    probe_suite_path = (
        _path(root["probe_suite"], "probe_suite", base)
        if "probe_suite" in root
        else None
    )
    probe_binding_paths = _probe_binding_paths(
        root.get("probe_bindings"),
        base=base,
        required=probe_suite_path is not None,
    )
    if probe_suite_path is None and probe_binding_paths:
        raise ExperimentError("probe_bindings requires probe_suite.")
    return ExperimentSpec(
        experiment_id=_identifier(root["experiment_id"], "experiment_id"),
        profile_paths=profile_paths,
        body_root=body_root,
        instance_command=command,
        models=models,
        reasoning_efforts=reasoning_efforts,
        identity_modes=identity_modes,
        evaluator=evaluator,
        population_path=population_path,
        counterfactual_pairs=counterfactual_pairs,
        repetitions=repetitions,
        timeout_seconds=timeout_seconds,
        probe_suite_path=probe_suite_path,
        probe_binding_paths=probe_binding_paths,
    )


def plan_experiment(
    spec: ExperimentSpec,
    *,
    batch_size: int | None = None,
    batch_index: int = 1,
) -> ExperimentPlan:
    loaded = _load_profiles(spec)
    profiles = tuple(profile for _, profile in loaded)
    _validate_profiles(profiles, spec.counterfactual_pairs)
    planned: list[PlannedRun] = []
    run_number = 0
    for profile_path, profile in loaded:
        for model in spec.models:
            for reasoning_effort in spec.reasoning_efforts:
                for identity_mode in spec.identity_modes:
                    for repetition in range(1, spec.repetitions + 1):
                        run_number += 1
                        run_id = f"run-{run_number:04d}"
                        planned.append(
                            PlannedRun(
                                run_id=run_id,
                                profile_path=profile_path,
                                profile=profile,
                                model=model,
                                reasoning_effort=reasoning_effort,
                                identity_mode=identity_mode,
                                repetition=repetition,
                                fingerprint=_run_fingerprint(
                                    spec,
                                    profile,
                                    model=model,
                                    reasoning_effort=reasoning_effort,
                                    identity_mode=identity_mode,
                                    repetition=repetition,
                                ),
                            )
                        )
    runs = tuple(planned)
    selected_batch_size = len(runs) if batch_size is None else _batch_size(batch_size)
    total_batches = math.ceil(len(runs) / selected_batch_size)
    if isinstance(batch_index, bool) or not isinstance(batch_index, int):
        raise ExperimentError("batch_index must be a positive integer.")
    if not 1 <= batch_index <= total_batches:
        raise ExperimentError(
            f"batch_index must be from 1 to {total_batches}; received {batch_index}."
        )
    start = (batch_index - 1) * selected_batch_size
    selected = runs[start : start + selected_batch_size]
    campaign_fingerprint = _fingerprint(
        {
            "experiment_id": spec.experiment_id,
            "runs": [run.to_dict() for run in runs],
            "counterfactual_pairs": [list(pair) for pair in spec.counterfactual_pairs],
            "population": _fingerprint_file_value(spec.population_path),
            "probe_suite": _fingerprint_file_value(spec.probe_suite_path),
        }
    )
    return ExperimentPlan(
        experiment_id=spec.experiment_id,
        campaign_fingerprint=campaign_fingerprint,
        runs=runs,
        selected_runs=selected,
        batch_size=selected_batch_size,
        batch_index=batch_index,
        total_batches=total_batches,
    )


def _load_profiles(
    spec: ExperimentSpec,
) -> tuple[tuple[Path, BenchmarkProfile], ...]:
    if spec.probe_suite_path is None:
        return tuple(
            (path, load_benchmark_profile(path)) for path in spec.profile_paths
        )
    binding_paths = dict(spec.probe_binding_paths)
    try:
        suite = load_probe_suite(spec.probe_suite_path)
        loaded: list[tuple[Path, BenchmarkProfile]] = []
        used_bindings: set[str] = set()
        for path in spec.profile_paths:
            identity = load_identity_profile(path)
            binding_path = binding_paths.get(identity.profile_id)
            if binding_path is None:
                raise ExperimentError(
                    "probe_bindings does not define profile "
                    f"{identity.profile_id!r}."
                )
            bindings = load_probe_bindings(binding_path)
            loaded.append(
                (path, compile_benchmark_profile(identity, suite, bindings))
            )
            used_bindings.add(identity.profile_id)
        unused = sorted(set(binding_paths) - used_bindings)
        if unused:
            raise ExperimentError(
                "probe_bindings contains profiles not in this experiment: "
                + ", ".join(unused)
                + "."
            )
        return tuple(loaded)
    except ProbeSuiteError as error:
        raise ExperimentError(str(error)) from error


def load_saved_experiment_runs(
    spec: ExperimentSpec, output_dir: Path, *, require_complete: bool = True
) -> tuple[ExperimentPlan, tuple[ExperimentRun, ...]]:
    """Load and verify atomic run artifacts without mutating experiment output."""
    output = output_dir.resolve()
    plan = plan_experiment(spec)
    plan_path = output / "experiment-plan.json"
    if not plan_path.exists():
        raise ExperimentError(
            f"Output directory {output} does not contain experiment-plan.json."
        )
    saved = _mapping(
        _read_json(plan_path, "saved experiment plan"), "saved experiment plan"
    )
    saved_experiment = _text(
        saved.get("experiment_id"), "saved experiment plan.experiment_id"
    )
    saved_fingerprint = _text(
        saved.get("campaign_fingerprint"),
        "saved experiment plan.campaign_fingerprint",
    )
    if saved_experiment != plan.experiment_id:
        raise ExperimentError(
            "Saved progress belongs to experiment "
            f"{saved_experiment!r}, not {plan.experiment_id!r}."
        )
    if saved_fingerprint != plan.campaign_fingerprint:
        raise ExperimentError(
            "Experiment manifest or profiles changed since this output was created."
        )
    expected = {f"{run.run_id}.json" for run in plan.runs}
    run_paths = tuple(sorted((output / "runs").glob("run-*.json")))
    unexpected = sorted(path.name for path in run_paths if path.name not in expected)
    if unexpected:
        raise ExperimentError(
            "Output directory contains run files outside the current plan: "
            + ", ".join(unexpected)
            + "."
        )
    loaded = _load_existing_runs(output, plan)
    if require_complete:
        missing = [run.run_id for run in plan.runs if run.run_id not in loaded]
        failed = [
            run_id for run_id, run in loaded.items() if run.report.errors != 0
        ]
        if missing or failed:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if failed:
                details.append("probe errors in " + ", ".join(failed))
            raise ExperimentError(
                "Saved experiment is not complete: " + "; ".join(details) + "."
            )
    runs = tuple(loaded[run.run_id] for run in plan.runs if run.run_id in loaded)
    return plan, runs


def run_experiment(
    spec: ExperimentSpec,
    output_dir: Path,
    *,
    batch_size: int | None = None,
    batch_index: int = 1,
    resume: bool = False,
) -> ExperimentReport:
    plan = plan_experiment(
        spec, batch_size=batch_size, batch_index=batch_index
    )
    profile_ids = tuple(dict.fromkeys(run.profile.profile_id for run in plan.runs))
    unique_profiles = tuple(
        next(run.profile for run in plan.runs if run.profile.profile_id == profile_id)
        for profile_id in profile_ids
    )
    population_groups = _population_groups(spec.population_path, unique_profiles)
    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    _prepare_output(output, plan, resume=resume)
    existing = _load_existing_runs(output, plan) if resume else {}
    started_at = _existing_started_at(output) if existing else _now()
    runs_by_id = dict(existing)
    with TemporaryDirectory(prefix=f"{spec.experiment_id}-state-") as temporary:
        state_parent = Path(temporary)
        for planned in plan.selected_runs:
            prior = runs_by_id.get(planned.run_id)
            if prior is not None and prior.report.errors == 0:
                continue
            state_home = state_parent / planned.run_id
            state_home.mkdir(mode=0o700)
            run = _run_condition(
                spec,
                planned.profile,
                profile_path=planned.profile_path,
                run_id=planned.run_id,
                state_home=state_home,
                model=planned.model,
                reasoning_effort=planned.reasoning_effort,
                identity_mode=planned.identity_mode,
                repetition=planned.repetition,
                fingerprint=planned.fingerprint,
            )
            runs_by_id[planned.run_id] = run
            _write_json(output / "runs" / f"{planned.run_id}.json", run.to_dict())
            _write_experiment_report(
                output,
                spec,
                plan,
                runs_by_id,
                unique_profiles,
                population_groups,
                started_at,
            )
    return _write_experiment_report(
        output,
        spec,
        plan,
        runs_by_id,
        unique_profiles,
        population_groups,
        started_at,
    )


def format_experiment_plan(plan: ExperimentPlan) -> str:
    lines = [
        "Identity Benchmark Plan",
        f"Experiment: {plan.experiment_id}",
        f"Total runs: {plan.total_runs}",
        f"Batch: {plan.batch_index}/{plan.total_batches}",
        f"Batch size: {plan.batch_size}",
        f"Selected runs: {len(plan.selected_runs)}",
        "",
        "Runs:",
    ]
    for run in plan.selected_runs:
        lines.append(
            f"- {run.run_id}: {run.profile.profile_id} / {run.model} / "
            f"{run.reasoning_effort} / {run.identity_mode} / "
            f"repetition {run.repetition}"
        )
    return "\n".join(lines)


def _prepare_output(
    output: Path, plan: ExperimentPlan, *, resume: bool
) -> None:
    plan_path = output / "experiment-plan.json"
    report_path = output / "experiment-report.json"
    run_paths = tuple(sorted((output / "runs").glob("run-*.json")))
    has_progress = plan_path.exists() or report_path.exists() or bool(run_paths)
    if has_progress and not resume:
        raise ExperimentError(
            f"Output directory {output} already contains experiment progress; "
            "use --resume or choose a new output directory."
        )
    if has_progress and not plan_path.exists():
        raise ExperimentError(
            f"Output directory {output} has legacy or incomplete progress without "
            "experiment-plan.json; choose a new output directory."
        )
    if plan_path.exists():
        saved = _read_json(plan_path, "saved experiment plan")
        root = _mapping(saved, "saved experiment plan")
        saved_experiment = _text(
            root.get("experiment_id"), "saved experiment plan.experiment_id"
        )
        saved_fingerprint = _text(
            root.get("campaign_fingerprint"),
            "saved experiment plan.campaign_fingerprint",
        )
        if saved_experiment != plan.experiment_id:
            raise ExperimentError(
                "Saved progress belongs to experiment "
                f"{saved_experiment!r}, not {plan.experiment_id!r}."
            )
        if saved_fingerprint != plan.campaign_fingerprint:
            raise ExperimentError(
                "Experiment manifest or profiles changed since this output was "
                "created; resume with the original inputs or choose a new output "
                "directory."
            )
    else:
        _write_json(plan_path, plan.to_dict(include_selection=False))

    expected = {f"{run.run_id}.json" for run in plan.runs}
    unexpected = sorted(path.name for path in run_paths if path.name not in expected)
    if unexpected:
        raise ExperimentError(
            "Output directory contains run files outside the current plan: "
            + ", ".join(unexpected)
            + "."
        )


def _load_existing_runs(
    output: Path, plan: ExperimentPlan
) -> dict[str, ExperimentRun]:
    expected = {run.run_id: run for run in plan.runs}
    loaded: dict[str, ExperimentRun] = {}
    for run_id, planned in expected.items():
        path = output / "runs" / f"{run_id}.json"
        if not path.exists():
            continue
        run = _parse_experiment_run(
            _read_json(path, f"saved run {run_id}"), label=f"saved run {run_id}"
        )
        mismatches = []
        for field, actual, wanted in (
            ("experiment_id", run.experiment_id, plan.experiment_id),
            ("run_id", run.run_id, planned.run_id),
            ("fingerprint", run.fingerprint, planned.fingerprint),
            ("profile_id", run.profile_id, planned.profile.profile_id),
            ("model", run.model, planned.model),
            ("reasoning_effort", run.reasoning_effort, planned.reasoning_effort),
            ("identity_mode", run.identity_mode, planned.identity_mode),
            ("repetition", run.repetition, planned.repetition),
        ):
            if actual != wanted:
                mismatches.append(field)
        if run.report.profile_id != run.profile_id:
            mismatches.append("report.profile_id")
        if mismatches:
            raise ExperimentError(
                f"Saved run {run_id} does not match the current plan: "
                + ", ".join(mismatches)
                + "."
            )
        try:
            validate_report_integrity(planned.profile, run.report)
        except ReportIntegrityError as error:
            raise ExperimentError(
                f"Saved run {run_id} report integrity failed: {error}."
            ) from error
        loaded[run_id] = run
    return loaded


def _parse_experiment_run(value: object, *, label: str) -> ExperimentRun:
    root = _mapping(value, label)
    _keys(
        root,
        label,
        required={
            "schema_version",
            "experiment_id",
            "run_id",
            "fingerprint",
            "profile_id",
            "model",
            "reasoning_effort",
            "identity_mode",
            "repetition",
            "report",
        },
    )
    if root["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
        raise ExperimentError(
            f"{label}.schema_version must be {EXPERIMENT_SCHEMA_VERSION}."
        )
    try:
        report = parse_benchmark_report(root["report"])
    except BenchmarkProfileError as error:
        raise ExperimentError(f"{label}.report is invalid: {error}") from error
    return ExperimentRun(
        experiment_id=_identifier(root["experiment_id"], f"{label}.experiment_id"),
        run_id=_identifier(root["run_id"], f"{label}.run_id"),
        fingerprint=_fingerprint_text(root["fingerprint"], f"{label}.fingerprint"),
        profile_id=_identifier(root["profile_id"], f"{label}.profile_id"),
        model=_text(root["model"], f"{label}.model"),
        reasoning_effort=_text(
            root["reasoning_effort"], f"{label}.reasoning_effort"
        ),
        identity_mode=_text(root["identity_mode"], f"{label}.identity_mode"),
        repetition=_positive_int(root["repetition"], f"{label}.repetition"),
        report=report,
    )


def _existing_started_at(output: Path) -> str:
    report_path = output / "experiment-report.json"
    if not report_path.exists():
        return _now()
    root = _mapping(
        _read_json(report_path, "saved experiment report"),
        "saved experiment report",
    )
    return _text(root.get("started_at"), "saved experiment report.started_at")


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"Could not load {label}: {error}") from error


def _run_fingerprint(
    spec: ExperimentSpec,
    profile: BenchmarkProfile,
    *,
    model: str,
    reasoning_effort: str,
    identity_mode: str,
    repetition: int,
) -> str:
    evaluator: JsonValue = None
    if spec.evaluator is not None:
        evaluator = {
            "id": spec.evaluator.evaluator_id,
            "harness": spec.evaluator.harness,
            "command": list(spec.evaluator.command),
            "model": spec.evaluator.model,
            "reasoning_effort": spec.evaluator.reasoning_effort,
            "rubric_version": spec.evaluator.rubric_version,
            "timeout_seconds": spec.evaluator.timeout_seconds,
        }
    return _fingerprint(
        {
            "experiment_id": spec.experiment_id,
            "profile": profile.to_dict(),
            "body_root": str(spec.body_root),
            "instance_command": list(spec.instance_command),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "identity_mode": identity_mode,
            "repetition": repetition,
            "timeout_seconds": spec.timeout_seconds,
            "evaluator": evaluator,
        }
    )


def _fingerprint(value: dict[str, JsonValue]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fingerprint_file_value(path: Path | None) -> JsonValue:
    if path is None:
        return None
    value = _read_json(path, "population index")
    if not isinstance(value, (dict, list, str, int, float, bool)) and value is not None:
        raise ExperimentError("population index must contain a JSON value.")
    return value  # type: ignore[return-value]


def _fingerprint_text(value: object, label: str) -> str:
    result = _text(value, label)
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ExperimentError(f"{label} must be a lowercase SHA-256 fingerprint.")
    return result


def _batch_size(value: object) -> int:
    return _positive_int(value, "batch_size")


def _write_experiment_report(
    output: Path,
    spec: ExperimentSpec,
    plan: ExperimentPlan,
    runs_by_id: dict[str, ExperimentRun],
    profiles: tuple[BenchmarkProfile, ...],
    population_groups: dict[str, tuple[str, str]],
    started_at: str,
) -> ExperimentReport:
    materialized = tuple(
        runs_by_id[planned.run_id]
        for planned in plan.runs
        if planned.run_id in runs_by_id
    )
    report = ExperimentReport(
        experiment_id=spec.experiment_id,
        profile_ids=tuple(profile.profile_id for profile in profiles),
        started_at=started_at,
        finished_at=_now(),
        runs=materialized,
        aggregates=_aggregates(materialized),
        identity_gains=_identity_gains(materialized),
        counterfactual_metrics=_counterfactual_metrics(
            materialized, profiles, spec.counterfactual_pairs
        ),
        population_aggregates=_population_aggregates(
            materialized, population_groups
        ),
        total_runs=plan.total_runs,
        completed_runs=sum(run.report.errors == 0 for run in materialized),
        selected_run_ids=tuple(run.run_id for run in plan.selected_runs),
        batch_size=plan.batch_size,
        batch_index=plan.batch_index,
        total_batches=plan.total_batches,
    )
    _write_json(output / "experiment-report.json", report.to_dict())
    return report


def format_experiment_report(report: ExperimentReport) -> str:
    lines = [
        "Identity Benchmark Experiment",
        f"Experiment: {report.experiment_id}",
        f"Profiles: {', '.join(report.profile_ids)}",
        f"Status: {'complete' if report.is_complete else 'partial'}",
        f"Progress: {report.completed_runs or sum(run.report.errors == 0 for run in report.runs)}/"
        f"{report.total_runs or len(report.runs)} completed "
        f"({len(report.runs)} attempted)",
        f"Batch: {report.batch_index}/{report.total_batches}",
        f"Evaluators: {', '.join(sorted({run.report.evaluator_id for run in report.runs}))}",
        f"Probe errors: {report.errors}",
        "",
        "Results:",
    ]
    for aggregate in report.aggregates:
        lines.append(
            "- "
            f"{aggregate['profile_id']} / {aggregate['model']} / "
            f"{aggregate['reasoning_effort']} / "
            f"{aggregate['identity_mode']}: {float(aggregate['mean_score']):.3f} "
            f"(n={aggregate['runs']}, "
            f"sd={float(aggregate['score_stddev']):.3f})"
        )
        metric_scores = aggregate.get("mean_metric_scores", {})
        if isinstance(metric_scores, dict):
            for metric, score in sorted(metric_scores.items()):
                lines.append(f"  - {metric}: {float(score):.3f}")
    if report.identity_gains:
        lines.extend(["", "Identity gain (identity condition - baseline):"])
        for gain in report.identity_gains:
            lines.append(
                f"- {gain['profile_id']} / {gain['model']} / "
                f"{gain['reasoning_effort']} / "
                f"{gain['identity_mode']} - {gain['baseline_mode']}: "
                f"{float(gain['gain']):+.3f}"
            )
    if report.counterfactual_metrics:
        lines.extend(["", "Counterfactual sensitivity:"])
        for metric in report.counterfactual_metrics:
            lines.append(
                f"- {metric['left_profile_id']} vs {metric['right_profile_id']} / "
                f"{metric['model']} / {metric['reasoning_effort']} / "
                f"{metric['identity_mode']}: "
                f"{float(metric['sensitivity']):.3f} "
                f"(paired accuracy={float(metric['paired_accuracy']):.3f}, "
                f"full score={float(metric['paired_full_score_rate']):.3f}, "
                f"response change={float(metric['response_change_rate']):.3f}, "
                f"n={metric['probe_pairs']})"
            )
    if report.population_aggregates:
        lines.extend(["", "Population strata:"])
        for aggregate in report.population_aggregates:
            lines.append(
                f"- {aggregate['category']} / {aggregate['stratum']} / "
                f"{aggregate['model']} / {aggregate['reasoning_effort']} / "
                f"{aggregate['identity_mode']}: "
                f"{float(aggregate['mean_score']):.3f} "
                f"(profiles={aggregate['profiles']}, runs={aggregate['runs']})"
            )
    return "\n".join(lines)


def _run_condition(
    spec: ExperimentSpec,
    profile: BenchmarkProfile,
    *,
    profile_path: Path,
    run_id: str,
    state_home: Path,
    model: str,
    reasoning_effort: str,
    identity_mode: str,
    repetition: int,
    fingerprint: str = "",
) -> ExperimentRun:
    replacements = {
        "body_root": str(spec.body_root),
        "profile": str(profile_path),
        "state_home": str(state_home),
        "identity_mode": identity_mode,
        "run_id": run_id,
    }
    command = tuple(_format_token(token, replacements) for token in spec.instance_command)
    instance = CommandInstance(
        command=command,
        instance_id=(
            f"{spec.experiment_id}:{profile.profile_id}:{model}:{reasoning_effort}:"
            f"{identity_mode}:r{repetition:02d}"
        ),
        timeout_seconds=spec.timeout_seconds,
        environment={
            "IDENTITY_BENCHMARK_STATE_HOME": str(state_home),
            "IDENTITY_BENCHMARK_MODEL": model,
            "IDENTITY_BENCHMARK_REASONING_EFFORT": reasoning_effort,
            "IDENTITY_BENCHMARK_IDENTITY_MODE": identity_mode,
            "IDENTITY_BENCHMARK_RUN_ID": run_id,
        },
        cwd=spec.body_root,
    )
    evaluator = None
    if spec.evaluator is not None:
        evaluator_state = state_home / "evaluator"
        evaluator_state.mkdir(mode=0o700)
        evaluator_replacements = {
            **replacements,
            "state_home": str(evaluator_state),
        }
        evaluator_command = tuple(
            _format_token(token, evaluator_replacements)
            for token in spec.evaluator.command
        )
        evaluator = CommandEvaluator(
            command=evaluator_command,
            evaluator_id=spec.evaluator.evaluator_id,
            timeout_seconds=spec.evaluator.timeout_seconds,
            environment={
                "IDENTITY_BENCHMARK_STATE_HOME": str(evaluator_state),
                "IDENTITY_BENCHMARK_EVALUATOR_ID": spec.evaluator.evaluator_id,
                "IDENTITY_BENCHMARK_EVALUATOR_HARNESS": spec.evaluator.harness,
                "IDENTITY_BENCHMARK_EVALUATOR_MODEL": spec.evaluator.model,
                "IDENTITY_BENCHMARK_EVALUATOR_REASONING_EFFORT": (
                    spec.evaluator.reasoning_effort
                ),
                "IDENTITY_BENCHMARK_EVALUATOR_RUBRIC_VERSION": (
                    spec.evaluator.rubric_version
                ),
                "IDENTITY_BENCHMARK_RUN_ID": run_id,
            },
            cwd=spec.body_root,
        )
    return ExperimentRun(
        run_id=run_id,
        profile_id=profile.profile_id,
        model=model,
        reasoning_effort=reasoning_effort,
        identity_mode=identity_mode,
        repetition=repetition,
        report=run_benchmark(profile, instance, evaluator=evaluator),
        experiment_id=spec.experiment_id,
        fingerprint=fingerprint,
    )


def _aggregates(runs: tuple[ExperimentRun, ...]) -> tuple[dict[str, JsonValue], ...]:
    keys = sorted(
        {
            (run.profile_id, run.model, run.reasoning_effort, run.identity_mode)
            for run in runs
        }
    )
    values: list[dict[str, JsonValue]] = []
    for profile_id, model, reasoning_effort, identity_mode in keys:
        selected = tuple(
            run
            for run in runs
            if (run.profile_id, run.model, run.reasoning_effort, run.identity_mode)
            == (profile_id, model, reasoning_effort, identity_mode)
        )
        scores = tuple(run.report.score for run in selected)
        metric_names = sorted(
            {
                metric
                for run in selected
                for metric in run.report.metric_scores
            }
        )
        values.append(
            {
                "profile_id": profile_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "identity_mode": identity_mode,
                "evaluator_id": selected[0].report.evaluator_id,
                "runs": len(scores),
                "mean_score": sum(scores) / len(scores),
                "min_score": min(scores),
                "max_score": max(scores),
                "score_stddev": _population_stddev(scores),
                "mean_metric_scores": {
                    metric: sum(
                        run.report.metric_scores[metric]
                        for run in selected
                        if metric in run.report.metric_scores
                    )
                    / sum(metric in run.report.metric_scores for run in selected)
                    for metric in metric_names
                },
                "probe_errors": sum(run.report.errors for run in selected),
            }
        )
    return tuple(values)


def _population_stddev(values: tuple[float, ...]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _evaluator_spec(value: object) -> EvaluatorSpec | None:
    if value is None:
        return None
    root = _mapping(value, "evaluator")
    _keys(
        root,
        "evaluator",
        required={
            "id",
            "harness",
            "command",
            "model",
            "reasoning_effort",
            "rubric_version",
        },
        optional={"timeout_seconds"},
    )
    command = tuple(
        _text(item, "evaluator.command[]")
        for item in _nonempty_list(root["command"], "evaluator.command")
    )
    return EvaluatorSpec(
        evaluator_id=_identifier(root["id"], "evaluator.id"),
        harness=_text(root["harness"], "evaluator.harness"),
        command=command,
        model=_text(root["model"], "evaluator.model"),
        reasoning_effort=_text(
            root["reasoning_effort"], "evaluator.reasoning_effort"
        ),
        rubric_version=_identifier(
            root["rubric_version"], "evaluator.rubric_version"
        ),
        timeout_seconds=_positive_number(
            root.get("timeout_seconds", 600.0), "evaluator.timeout_seconds"
        ),
    )


def _population_groups(
    path: Path | None,
    profiles: tuple[BenchmarkProfile, ...],
) -> dict[str, tuple[str, str]]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExperimentError(f"Could not load population index: {error}") from error
    root = _mapping(value, "population index")
    raw_profiles = root.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ExperimentError("population index profiles must be a non-empty list.")
    groups: dict[str, tuple[str, str]] = {}
    for index, item in enumerate(raw_profiles):
        entry = _mapping(item, f"population index profiles[{index}]")
        profile_id = _identifier(
            entry.get("profile_id"),
            f"population index profiles[{index}].profile_id",
        )
        if profile_id in groups:
            raise ExperimentError(
                f"population index repeats profile_id {profile_id!r}."
            )
        groups[profile_id] = (
            _identifier(
                entry.get("category"),
                f"population index profiles[{index}].category",
            ),
            _identifier(
                entry.get("stratum"),
                f"population index profiles[{index}].stratum",
            ),
        )
    selected_ids = {profile.profile_id for profile in profiles}
    missing = sorted(selected_ids - set(groups))
    if missing:
        raise ExperimentError(
            "population index is missing experiment profiles: "
            + ", ".join(missing)
            + "."
        )
    return {profile_id: groups[profile_id] for profile_id in selected_ids}


def _population_aggregates(
    runs: tuple[ExperimentRun, ...],
    groups: dict[str, tuple[str, str]],
) -> tuple[dict[str, JsonValue], ...]:
    if not groups:
        return ()
    keys = sorted(
        {
            (
                groups[run.profile_id][0],
                groups[run.profile_id][1],
                run.model,
                run.reasoning_effort,
                run.identity_mode,
            )
            for run in runs
        }
    )
    aggregates: list[dict[str, JsonValue]] = []
    for category, stratum, model, reasoning_effort, identity_mode in keys:
        selected = tuple(
            run
            for run in runs
            if groups[run.profile_id] == (category, stratum)
            and run.model == model
            and run.reasoning_effort == reasoning_effort
            and run.identity_mode == identity_mode
        )
        scores = tuple(run.report.score for run in selected)
        metric_names = sorted(
            {
                metric
                for run in selected
                for metric in run.report.metric_scores
            }
        )
        aggregates.append(
            {
                "category": category,
                "stratum": stratum,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "identity_mode": identity_mode,
                "evaluator_id": selected[0].report.evaluator_id,
                "profiles": len({run.profile_id for run in selected}),
                "runs": len(selected),
                "mean_score": sum(scores) / len(scores),
                "score_stddev": _population_stddev(scores),
                "mean_metric_scores": {
                    metric: sum(
                        run.report.metric_scores[metric]
                        for run in selected
                        if metric in run.report.metric_scores
                    )
                    / sum(
                        metric in run.report.metric_scores for run in selected
                    )
                    for metric in metric_names
                },
                "probe_errors": sum(run.report.errors for run in selected),
            }
        )
    return tuple(aggregates)


def _identity_gains(runs: tuple[ExperimentRun, ...]) -> tuple[dict[str, JsonValue], ...]:
    lookup = {
        (
            str(item["profile_id"]),
            str(item["model"]),
            str(item["reasoning_effort"]),
            str(item["identity_mode"]),
        ): float(item["mean_score"])
        for item in _aggregates(runs)
    }
    pairs = sorted(
        {(run.profile_id, run.model, run.reasoning_effort) for run in runs}
    )
    gains: list[dict[str, JsonValue]] = []
    mode_pairs = (
        ("uninstalled", "installed"),
        ("none", "full-context"),
    )
    for profile_id, model, reasoning_effort in pairs:
        for baseline_mode, identity_mode in mode_pairs:
            baseline = lookup.get(
                (profile_id, model, reasoning_effort, baseline_mode)
            )
            identity = lookup.get(
                (profile_id, model, reasoning_effort, identity_mode)
            )
            if baseline is None or identity is None:
                continue
            item: dict[str, JsonValue] = {
                "profile_id": profile_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "baseline_mode": baseline_mode,
                "identity_mode": identity_mode,
                "baseline_score": baseline,
                "identity_score": identity,
                "gain": identity - baseline,
            }
            if identity_mode == "full-context":
                item["full_context_score"] = identity
            gains.append(item)
            break
    return tuple(gains)


def _counterfactual_metrics(
    runs: tuple[ExperimentRun, ...],
    profiles: tuple[BenchmarkProfile, ...],
    pairs: tuple[tuple[str, str], ...],
) -> tuple[dict[str, JsonValue], ...]:
    if not pairs:
        return ()
    profiles_by_id = {profile.profile_id: profile for profile in profiles}
    runs_by_key = {
        (
            run.profile_id,
            run.model,
            run.reasoning_effort,
            run.identity_mode,
            run.repetition,
        ): run
        for run in runs
    }
    conditions = sorted(
        {
            (run.model, run.reasoning_effort, run.identity_mode)
            for run in runs
        }
    )
    metrics: list[dict[str, JsonValue]] = []
    for left_id, right_id in pairs:
        left_probes = {
            probe.id: probe for probe in profiles_by_id[left_id].probes
        }
        right_probes = {
            probe.id: probe for probe in profiles_by_id[right_id].probes
        }
        probe_ids = counterfactual_probe_ids(
            profiles_by_id[left_id], profiles_by_id[right_id]
        )
        for model, reasoning_effort, identity_mode in conditions:
            probe_pairs = 0
            both_correct = 0
            both_full_score = 0
            changed = 0
            sensitive = 0
            full_score_sensitive = 0
            run_pairs = 0
            for repetition in sorted({run.repetition for run in runs}):
                left = runs_by_key.get(
                    (left_id, model, reasoning_effort, identity_mode, repetition)
                )
                right = runs_by_key.get(
                    (right_id, model, reasoning_effort, identity_mode, repetition)
                )
                if left is None or right is None:
                    continue
                run_pairs += 1
                left_results = {result.probe_id: result for result in left.report.results}
                right_results = {
                    result.probe_id: result for result in right.report.results
                }
                for probe_id in probe_ids:
                    left_result = left_results[probe_id]
                    right_result = right_results[probe_id]
                    pair_correct = passes_counterfactual_gates(
                        left_probes[probe_id], left_result.response
                    ) and passes_counterfactual_gates(
                        right_probes[probe_id], right_result.response
                    )
                    pair_full_score = (
                        left_result.score == 1.0 and right_result.score == 1.0
                    )
                    response_changed = normalize_text(
                        left_result.response
                    ) != normalize_text(right_result.response)
                    probe_pairs += 1
                    both_correct += int(pair_correct)
                    both_full_score += int(pair_full_score)
                    changed += int(response_changed)
                    sensitive += int(pair_correct and response_changed)
                    full_score_sensitive += int(
                        pair_full_score and response_changed
                    )
            if not run_pairs:
                continue
            metrics.append(
                {
                    "left_profile_id": left_id,
                    "right_profile_id": right_id,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "identity_mode": identity_mode,
                    "run_pairs": run_pairs,
                    "probe_pairs": probe_pairs,
                    "paired_accuracy": both_correct / probe_pairs,
                    "paired_full_score_rate": both_full_score / probe_pairs,
                    "response_change_rate": changed / probe_pairs,
                    "sensitivity": sensitive / probe_pairs,
                    "full_score_sensitivity": full_score_sensitive / probe_pairs,
                }
            )
    return tuple(metrics)


def passes_counterfactual_gates(probe: Probe, response: str) -> bool:
    """Return whether a response satisfies every gated counterfactual oracle."""
    scorer = DeterministicScorer()
    gates = tuple(
        expectation for expectation in probe.expectations if expectation.gate
    )
    return bool(gates) and all(
        scorer.score(response, expectation).passed for expectation in gates
    )


def _validate_profiles(
    profiles: tuple[BenchmarkProfile, ...],
    pairs: tuple[tuple[str, str], ...],
) -> None:
    profile_ids = tuple(profile.profile_id for profile in profiles)
    if len(profile_ids) != len(set(profile_ids)):
        raise ExperimentError("profiles must have unique profile_id values.")
    profiles_by_id = {profile.profile_id: profile for profile in profiles}
    for left_id, right_id in pairs:
        missing = [item for item in (left_id, right_id) if item not in profiles_by_id]
        if missing:
            raise ExperimentError(
                "counterfactual_pairs references profiles not in this experiment: "
                + ", ".join(missing)
                + "."
            )
        counterfactual_probe_ids(
            profiles_by_id[left_id], profiles_by_id[right_id]
        )


def counterfactual_probe_ids(
    left: BenchmarkProfile, right: BenchmarkProfile
) -> tuple[str, ...]:
    """Validate a profile pair and return its matched counterfactual probes."""
    left_probes = {
        probe.id: probe for probe in left.probes if "counterfactual" in probe.tags
    }
    right_probes = {
        probe.id: probe for probe in right.probes if "counterfactual" in probe.tags
    }
    if not left_probes or set(left_probes) != set(right_probes):
        raise ExperimentError(
            f"counterfactual pair {left.profile_id!r}/{right.profile_id!r} must "
            "have the same non-empty set of counterfactual probe IDs."
        )
    for probe_id in sorted(left_probes):
        left_probe = left_probes[probe_id]
        right_probe = right_probes[probe_id]
        if left_probe.messages != right_probe.messages:
            raise ExperimentError(
                f"counterfactual probe {probe_id!r} must use identical messages."
            )
        left_expectations = tuple(
            (item.type, normalize_text(item.value), item.gate, item.weight)
            for item in left_probe.expectations
        )
        right_expectations = tuple(
            (item.type, normalize_text(item.value), item.gate, item.weight)
            for item in right_probe.expectations
        )
        if left_expectations == right_expectations:
            raise ExperimentError(
                f"counterfactual probe {probe_id!r} must have different expectations."
            )
        left_gates = tuple(
            (item.type, normalize_text(item.value), item.weight)
            for item in left_probe.expectations
            if item.gate
        )
        right_gates = tuple(
            (item.type, normalize_text(item.value), item.weight)
            for item in right_probe.expectations
            if item.gate
        )
        if not left_gates or not right_gates:
            raise ExperimentError(
                f"counterfactual probe {probe_id!r} must declare at least one "
                "gated expectation for each profile."
            )
        if left_gates == right_gates:
            raise ExperimentError(
                f"counterfactual probe {probe_id!r} must have different gated "
                "expectations."
            )
    return tuple(sorted(left_probes))


def _format_token(token: str, replacements: dict[str, str]) -> str:
    try:
        return token.format_map(replacements)
    except KeyError as error:
        raise ExperimentError(
            f"instance_command uses unknown placeholder {error.args[0]!r}."
        ) from error


def _write_json(path: Path, value: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentError(f"{label} must be an object.")
    return value


def _keys(
    value: dict[str, Any],
    label: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    if missing:
        raise ExperimentError(f"{label} is missing required fields: {', '.join(missing)}.")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ExperimentError(f"{label} has unknown fields: {', '.join(unexpected)}.")


def _nonempty_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ExperimentError(f"{label} must be a non-empty list.")
    return value


def _profile_paths(root: dict[str, Any], base: Path) -> tuple[Path, ...]:
    has_profile = "profile" in root
    has_profiles = "profiles" in root
    if has_profile == has_profiles:
        raise ExperimentError(
            "experiment manifest must define exactly one of profile or profiles."
        )
    if has_profile:
        return (_path(root["profile"], "profile", base),)
    paths = tuple(
        _path(item, "profiles[]", base)
        for item in _nonempty_list(root["profiles"], "profiles")
    )
    if len(paths) != len(set(paths)):
        raise ExperimentError("profiles must not contain duplicate paths.")
    return paths


def _probe_binding_paths(
    value: object,
    *,
    base: Path,
    required: bool,
) -> tuple[tuple[str, Path], ...]:
    if value is None:
        if required:
            raise ExperimentError("probe_suite requires probe_bindings.")
        return ()
    root = _mapping(value, "probe_bindings")
    if not root:
        raise ExperimentError("probe_bindings must not be empty.")
    return tuple(
        sorted(
            (
                _identifier(profile_id, "probe_bindings profile id"),
                _path(path, f"probe_bindings.{profile_id}", base),
            )
            for profile_id, path in root.items()
        )
    )


def _counterfactual_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ExperimentError("counterfactual_pairs must be a list.")
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        label = f"counterfactual_pairs[{index}]"
        if not isinstance(item, list) or len(item) != 2:
            raise ExperimentError(f"{label} must contain exactly two profile IDs.")
        pair = (
            _identifier(item[0], f"{label}[0]"),
            _identifier(item[1], f"{label}[1]"),
        )
        if pair[0] == pair[1]:
            raise ExperimentError(f"{label} must reference two different profiles.")
        pairs.append(pair)
    canonical = {tuple(sorted(pair)) for pair in pairs}
    if len(canonical) != len(pairs):
        raise ExperimentError("counterfactual_pairs must not contain duplicate pairs.")
    return tuple(pairs)


def _unique_texts(value: object, label: str) -> tuple[str, ...]:
    values = tuple(_text(item, f"{label}[]") for item in _nonempty_list(value, label))
    if len(values) != len(set(values)):
        raise ExperimentError(f"{label} must not contain duplicates.")
    return values


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise ExperimentError(f"{label} must be a portable identifier.")
    return text


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{label} must be non-empty text.")
    return value.strip()


def _path(value: object, label: str, base: Path) -> Path:
    path = Path(_text(value, label)).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExperimentError(f"{label} must be a positive integer.")
    return value


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentError(f"{label} must be a positive number.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ExperimentError(f"{label} must be a positive finite number.")
    return number


def _now() -> str:
    return datetime.now(UTC).isoformat()
