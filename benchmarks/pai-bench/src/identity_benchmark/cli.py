from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from identity_benchmark.adapters import CommandInstance, InstanceError
from identity_benchmark.contracts import BenchmarkProfileError, BenchmarkReport, load_benchmark_profile
from identity_benchmark.experiments import (
    ExperimentError,
    format_experiment_plan,
    format_experiment_report,
    load_experiment_spec,
    plan_experiment,
    run_experiment,
)
from identity_benchmark.runner import run_benchmark
from identity_benchmark.rescore import RescoreError, rescore_saved_report
from identity_benchmark.statistics import (
    DEFAULT_BOOTSTRAP_SAMPLES,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE_LEVEL,
    StatisticalAnalysisError,
    analyze_experiment,
    format_statistical_analysis,
    write_statistical_analysis,
)
from identity_benchmark.population import (
    DEFAULT_SEED,
    DEFAULT_SIZE,
    PopulationError,
    write_population,
)


def main(argv: list[str] | None = None) -> None:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    instance_command: tuple[str, ...] = ()
    if raw_args and raw_args[0] == "run" and "--" in raw_args:
        separator = raw_args.index("--")
        instance_command = tuple(raw_args[separator + 1 :])
        raw_args = raw_args[:separator]
    parser = _parser()
    args = parser.parse_args(raw_args)
    try:
        if args.action == "validate":
            profile = load_benchmark_profile(args.profile)
            print(
                f"Valid identity benchmark profile {profile.profile_id}: "
                f"{len(profile.statements)} statements, {len(profile.probes)} probes."
            )
            return
        if args.action == "matrix":
            spec = load_experiment_spec(args.experiment)
            if args.plan:
                plan = plan_experiment(
                    spec,
                    batch_size=args.batch_size,
                    batch_index=args.batch_index,
                )
                print(format_experiment_plan(plan))
                return
            if args.output_dir is None:
                parser.error("matrix requires --output-dir unless --plan is used")
            report = run_experiment(
                spec,
                args.output_dir,
                batch_size=args.batch_size,
                batch_index=args.batch_index,
                resume=args.resume,
            )
            print(format_experiment_report(report))
            return
        if args.action == "rescore":
            profile = load_benchmark_profile(args.profile)
            report = rescore_saved_report(profile, args.report)
            if args.json_out:
                _write_report(args.json_out, report)
            print(format_report(report))
            return
        if args.action == "bootstrap":
            spec = load_experiment_spec(args.experiment)
            comparison_spec = (
                load_experiment_spec(args.compare_experiment)
                if args.compare_experiment is not None
                else None
            )
            report = analyze_experiment(
                spec,
                args.report_dir,
                comparison_spec=comparison_spec,
                comparison_report_dir=args.compare_report_dir,
                samples=args.samples,
                confidence_level=args.confidence_level,
                seed=args.seed,
            )
            write_statistical_analysis(args.output_dir, report)
            print(format_statistical_analysis(report))
            return
        if args.action == "generate-population":
            paths = write_population(
                args.output_dir,
                size=args.size,
                seed=args.seed,
                check=args.check,
            )
            verb = "Verified" if args.check else "Generated"
            print(
                f"{verb} synthetic identity population: {args.size} profiles, "
                f"seed {args.seed}, {len(paths)} files."
            )
            return
        profile = load_benchmark_profile(args.profile)
        if not instance_command:
            parser.error("run requires an instance command after '--'")
        instance = CommandInstance(
            command=instance_command,
            instance_id=args.instance_id,
            timeout_seconds=args.timeout,
        )
        report = run_benchmark(profile, instance)
        if args.json_out:
            _write_report(args.json_out, report)
        print(format_report(report))
        if args.minimum_score is not None and report.score < args.minimum_score:
            raise SystemExit(1)
    except (
        BenchmarkProfileError,
        ExperimentError,
        InstanceError,
        PopulationError,
        RescoreError,
        StatisticalAnalysisError,
        OSError,
    ) as error:
        parser.exit(2, f"identity-benchmark: {error}\n")


def format_report(report: BenchmarkReport) -> str:
    lines = [
        "Identity Benchmark Report",
        f"Profile: {report.profile_id}",
        f"Instance: {report.instance_id}",
        f"Evaluator: {report.evaluator_id}",
        f"Score: {report.score:.3f}",
        f"Errors: {report.errors}",
        "",
        "Dimensions:",
    ]
    lines.extend(
        f"- {dimension}: {score:.3f}"
        for dimension, score in sorted(report.dimension_scores.items())
    )
    lines.extend(["", "Metrics:"])
    lines.extend(
        f"- {metric}: {score:.3f}"
        for metric, score in sorted(report.metric_scores.items())
    )
    lines.extend(["", "Probes:"])
    for result in report.results:
        status = "error" if result.error else f"{result.score:.3f}"
        lines.append(f"- {result.probe_id} [{result.dimension}]: {status}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="identity-benchmark",
        description="Run a provider-neutral identity consistency benchmark.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    validate = subparsers.add_parser("validate", help="validate one benchmark profile")
    validate.add_argument("profile", type=Path)
    matrix = subparsers.add_parser(
        "matrix", help="run a local matrix from one experiment manifest"
    )
    matrix.add_argument("experiment", type=Path)
    matrix.add_argument("--output-dir", type=Path)
    matrix.add_argument(
        "--batch-size",
        type=_positive_integer,
        help="run at most this many atomic conditions in the selected batch",
    )
    matrix.add_argument(
        "--batch-index",
        type=_positive_integer,
        default=1,
        help="one-based batch to run (default: 1)",
    )
    matrix.add_argument(
        "--resume",
        action="store_true",
        help="reuse verified completed runs and retry incomplete runs",
    )
    matrix.add_argument(
        "--plan",
        action="store_true",
        help="print the selected batch without running it",
    )
    rescore = subparsers.add_parser(
        "rescore", help="rescore saved responses against a compatible profile"
    )
    rescore.add_argument("profile", type=Path)
    rescore.add_argument("report", type=Path)
    rescore.add_argument("--json-out", type=Path)
    bootstrap = subparsers.add_parser(
        "bootstrap",
        help="estimate clustered confidence intervals from saved matrix runs",
    )
    bootstrap.add_argument("experiment", type=Path)
    bootstrap.add_argument("report_dir", type=Path)
    bootstrap.add_argument("--output-dir", type=Path, required=True)
    bootstrap.add_argument("--compare-experiment", type=Path)
    bootstrap.add_argument("--compare-report-dir", type=Path)
    bootstrap.add_argument(
        "--samples", type=_bootstrap_samples, default=DEFAULT_BOOTSTRAP_SAMPLES
    )
    bootstrap.add_argument("--seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    bootstrap.add_argument(
        "--confidence-level",
        type=_confidence_level,
        default=DEFAULT_CONFIDENCE_LEVEL,
    )
    population = subparsers.add_parser(
        "generate-population",
        help="generate or verify a deterministic synthetic identity population",
    )
    population.add_argument("output_dir", type=Path)
    population.add_argument("--size", type=int, default=DEFAULT_SIZE)
    population.add_argument("--seed", type=int, default=DEFAULT_SEED)
    population.add_argument("--check", action="store_true")
    run = subparsers.add_parser("run", help="run a profile against one target instance")
    run.add_argument("profile", type=Path)
    run.add_argument("--instance-id", required=True)
    run.add_argument("--timeout", type=float, default=120.0)
    run.add_argument("--minimum-score", type=_unit_score)
    run.add_argument("--json-out", type=Path)
    return parser


def _unit_score(value: str) -> float:
    score = float(value)
    if not 0 <= score <= 1:
        raise argparse.ArgumentTypeError("minimum score must be between 0 and 1")
    return score


def _positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return number


def _bootstrap_samples(value: str) -> int:
    number = int(value)
    if number < 2:
        raise argparse.ArgumentTypeError("bootstrap samples must be at least 2")
    return number


def _confidence_level(value: str) -> float:
    confidence = float(value)
    if not 0 < confidence < 1:
        raise argparse.ArgumentTypeError("confidence level must be between 0 and 1")
    return confidence


def _write_report(path: Path, report: BenchmarkReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    sys.exit(main())
