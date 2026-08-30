from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import statistics as descriptive_statistics
from typing import Callable, Sequence

from identity_benchmark.contracts import BenchmarkProfile, JsonValue
from identity_benchmark.experiments import (
    ExperimentPlan,
    ExperimentRun,
    ExperimentSpec,
    counterfactual_probe_ids,
    load_saved_experiment_runs,
    passes_counterfactual_gates,
)
from identity_benchmark.runner import HEADLINE_EXCLUDED_TAGS
from identity_benchmark.scoring import normalize_text


STATISTICS_SCHEMA_VERSION = 1
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_BOOTSTRAP_SEED = 271_828
DEFAULT_CONFIDENCE_LEVEL = 0.95


class StatisticalAnalysisError(ValueError):
    """Raised when saved experiment results cannot support an analysis."""


@dataclass(frozen=True)
class _Observation:
    cluster: str
    family: str
    values: tuple[float, ...]
    weight: float = 1.0


Statistic = Callable[[Sequence[_Observation]], float | None]
Condition = tuple[str, str, str]
RunKey = tuple[str, str, str, str, int]


def analyze_experiment(
    spec: ExperimentSpec,
    report_dir: Path,
    *,
    comparison_spec: ExperimentSpec | None = None,
    comparison_report_dir: Path | None = None,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, JsonValue]:
    """Analyze saved runs with crossed identity/probe-family bootstraps."""
    _validate_configuration(
        samples=samples,
        confidence_level=confidence_level,
        comparison_spec=comparison_spec,
        comparison_report_dir=comparison_report_dir,
    )
    primary_plan, primary_runs = load_saved_experiment_runs(spec, report_dir)
    primary_profiles = _profiles(primary_plan)
    primary_clusters = _identity_clusters(primary_profiles, spec.counterfactual_pairs)
    primary = _analyze_collection(
        spec,
        primary_plan,
        primary_runs,
        primary_profiles,
        primary_clusters,
        samples=samples,
        confidence_level=confidence_level,
        seed=seed,
        label="primary",
    )
    report: dict[str, JsonValue] = {
        "schema_version": STATISTICS_SCHEMA_VERSION,
        "method": {
            "name": "crossed-cluster-percentile-bootstrap",
            "algorithm_version": "pai-bootstrap-v1",
            "samples": samples,
            "confidence_level": confidence_level,
            "seed": seed,
            "identity_resampling_unit": (
                "declared counterfactual pair; otherwise individual profile"
            ),
            "probe_family_resampling_unit": "probe_id",
            "resampling": (
                "identity clusters and probe families are independently sampled "
                "with replacement; paired model and judge comparisons share draws"
            ),
            "interval": "percentile",
            "scope": (
                "sampling uncertainty over observed identities and probe families; "
                "not target decoding variance"
            ),
        },
        "primary": primary,
    }
    if comparison_spec is not None and comparison_report_dir is not None:
        if spec.counterfactual_pairs != comparison_spec.counterfactual_pairs:
            raise StatisticalAnalysisError(
                "Comparison experiment must declare the same counterfactual pairs."
            )
        comparison_plan, comparison_runs = load_saved_experiment_runs(
            comparison_spec, comparison_report_dir
        )
        comparison_profiles = _profiles(comparison_plan)
        _validate_same_response_comparison(
            primary_profiles,
            primary_runs,
            comparison_profiles,
            comparison_runs,
        )
        comparison_clusters = _identity_clusters(
            comparison_profiles, comparison_spec.counterfactual_pairs
        )
        comparison = _analyze_collection(
            comparison_spec,
            comparison_plan,
            comparison_runs,
            comparison_profiles,
            comparison_clusters,
            samples=samples,
            confidence_level=confidence_level,
            seed=seed,
            label="comparison",
        )
        report["comparison"] = comparison
        report["cross_judge"] = _cross_judge_analysis(
            primary_runs,
            comparison_runs,
            primary_clusters,
            samples=samples,
            confidence_level=confidence_level,
            seed=seed,
        )
    return report


def write_statistical_analysis(
    output_dir: Path, report: dict[str, JsonValue]
) -> tuple[Path, Path]:
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "bootstrap-report.json"
    markdown_path = output / "bootstrap-report.md"
    _atomic_write(
        json_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(markdown_path, format_statistical_analysis(report) + "\n")
    return json_path, markdown_path


def format_statistical_analysis(report: dict[str, JsonValue]) -> str:
    method = _dict(report["method"])
    primary = _dict(report["primary"])
    lines = [
        "# Identity benchmark bootstrap analysis",
        "",
        f"- Method: `{method['name']}` / `{method['algorithm_version']}`",
        f"- Resamples: {method['samples']}",
        f"- Confidence level: {float(method['confidence_level']) * 100:.1f}%",
        f"- Seed: {method['seed']}",
        f"- Scope: {method['scope']}",
        "",
    ]
    lines.extend(_format_collection("Primary evaluator", primary))
    comparison_value = report.get("comparison")
    if isinstance(comparison_value, dict):
        lines.extend(_format_collection("Comparison evaluator", comparison_value))
    cross_value = report.get("cross_judge")
    if isinstance(cross_value, dict):
        lines.extend(_format_cross_judge(cross_value))
    lines.extend(
        [
            "## Interpretation",
            "",
            "Intervals resample observed identity clusters and probe families. They "
            "do not estimate target-model decoding variance because each frozen "
            "condition has one target response per identity and probe.",
        ]
    )
    return "\n".join(lines)


def _analyze_collection(
    spec: ExperimentSpec,
    plan: ExperimentPlan,
    runs: tuple[ExperimentRun, ...],
    profiles: dict[str, BenchmarkProfile],
    clusters: dict[str, str],
    *,
    samples: int,
    confidence_level: float,
    seed: int,
    label: str,
) -> dict[str, JsonValue]:
    headline = _score_observations(runs, profiles, clusters, headline_only=True)
    by_condition = _group_by_condition(headline)
    targets = []
    for condition, observations in sorted(by_condition.items()):
        estimate = _bootstrap_estimate(
            observations,
            _score_mean,
            samples=samples,
            confidence_level=confidence_level,
            seed=_derived_seed(seed, f"{label}:target:{condition}"),
        )
        targets.append(
            {
                **_condition_dict(condition),
                **_sample_description(observations),
                **estimate,
            }
        )
    differences = _target_differences(
        runs,
        profiles,
        clusters,
        samples=samples,
        confidence_level=confidence_level,
        seed=seed,
        label=label,
    )
    counterfactual = _counterfactual_analysis(
        spec,
        runs,
        profiles,
        clusters,
        samples=samples,
        confidence_level=confidence_level,
        seed=seed,
        label=label,
    )
    return {
        "experiment_id": spec.experiment_id,
        "campaign_fingerprint": plan.campaign_fingerprint,
        "evaluator_ids": sorted({run.report.evaluator_id for run in runs}),
        "runs": len(runs),
        "identity_clusters": len(set(clusters.values())),
        "profiles": len(profiles),
        "target_conditions": targets,
        "paired_target_differences": differences,
        "counterfactual": counterfactual,
    }


def _score_observations(
    runs: tuple[ExperimentRun, ...],
    profiles: dict[str, BenchmarkProfile],
    clusters: dict[str, str],
    *,
    headline_only: bool,
) -> list[tuple[Condition, _Observation, RunKey]]:
    observations = []
    for run in runs:
        profile = profiles[run.profile_id]
        probes = {probe.id: probe for probe in profile.probes}
        condition = _condition(run)
        run_key = _run_key(run)
        for result in run.report.results:
            probe = probes[result.probe_id]
            if headline_only and (
                result.dimension == "capability"
                or HEADLINE_EXCLUDED_TAGS.intersection(probe.tags)
            ):
                continue
            observations.append(
                (
                    condition,
                    _Observation(
                        cluster=clusters[run.profile_id],
                        family=result.probe_id,
                        values=(result.score,),
                        weight=result.weight,
                    ),
                    run_key,
                )
            )
    return observations


def _target_differences(
    runs: tuple[ExperimentRun, ...],
    profiles: dict[str, BenchmarkProfile],
    clusters: dict[str, str],
    *,
    samples: int,
    confidence_level: float,
    seed: int,
    label: str,
) -> list[dict[str, JsonValue]]:
    raw = _score_observations(runs, profiles, clusters, headline_only=True)
    lookup: dict[tuple[Condition, str, str, int], _Observation] = {}
    for condition, observation, run_key in raw:
        profile_id, _, _, _, repetition = run_key
        lookup[(condition, profile_id, observation.family, repetition)] = observation
    grouped: dict[tuple[str, str], set[str]] = {}
    for model, reasoning_effort, identity_mode in {item[0] for item in raw}:
        grouped.setdefault((reasoning_effort, identity_mode), set()).add(model)
    rows = []
    for (reasoning_effort, identity_mode), models in sorted(grouped.items()):
        for left_model, right_model in itertools.combinations(sorted(models), 2):
            left_condition = (left_model, reasoning_effort, identity_mode)
            right_condition = (right_model, reasoning_effort, identity_mode)
            left_keys = {
                (profile_id, family, repetition)
                for condition, profile_id, family, repetition in lookup
                if condition == left_condition
            }
            right_keys = {
                (profile_id, family, repetition)
                for condition, profile_id, family, repetition in lookup
                if condition == right_condition
            }
            if left_keys != right_keys:
                raise StatisticalAnalysisError(
                    "Target conditions do not have identical paired observations: "
                    f"{left_model} vs {right_model}."
                )
            observations = []
            for profile_id, family, repetition in sorted(left_keys):
                left = lookup[
                    (left_condition, profile_id, family, repetition)
                ]
                right = lookup[
                    (right_condition, profile_id, family, repetition)
                ]
                if left.weight != right.weight:
                    raise StatisticalAnalysisError(
                        f"Probe weight mismatch for {profile_id}/{family}."
                    )
                observations.append(
                    _Observation(
                        cluster=clusters[profile_id],
                        family=family,
                        values=(right.values[0] - left.values[0],),
                        weight=left.weight,
                    )
                )
            interval = _bootstrap_estimate(
                observations,
                _score_mean,
                samples=samples,
                confidence_level=confidence_level,
                seed=_derived_seed(
                    seed,
                    f"{label}:difference:{left_condition}:{right_condition}",
                ),
            )
            rows.append(
                {
                    "left_model": left_model,
                    "right_model": right_model,
                    "contrast": "right_minus_left",
                    "reasoning_effort": reasoning_effort,
                    "identity_mode": identity_mode,
                    **_sample_description(observations),
                    **interval,
                }
            )
    return rows


def _counterfactual_analysis(
    spec: ExperimentSpec,
    runs: tuple[ExperimentRun, ...],
    profiles: dict[str, BenchmarkProfile],
    clusters: dict[str, str],
    *,
    samples: int,
    confidence_level: float,
    seed: int,
    label: str,
) -> list[dict[str, JsonValue]]:
    if not spec.counterfactual_pairs:
        return []
    run_lookup = {_run_key(run): run for run in runs}
    conditions = sorted({_condition(run) for run in runs})
    repetitions = sorted({run.repetition for run in runs})
    rows = []
    metric_names = (
        "paired_accuracy",
        "response_change_rate",
        "sensitivity",
        "full_score_sensitivity",
    )
    for condition in conditions:
        observations = []
        model, reasoning_effort, identity_mode = condition
        for left_id, right_id in spec.counterfactual_pairs:
            left_probes = {probe.id: probe for probe in profiles[left_id].probes}
            right_probes = {probe.id: probe for probe in profiles[right_id].probes}
            probe_ids = counterfactual_probe_ids(
                profiles[left_id], profiles[right_id]
            )
            for repetition in repetitions:
                left = run_lookup.get(
                    (left_id, model, reasoning_effort, identity_mode, repetition)
                )
                right = run_lookup.get(
                    (right_id, model, reasoning_effort, identity_mode, repetition)
                )
                if left is None or right is None:
                    continue
                left_results = {
                    result.probe_id: result for result in left.report.results
                }
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
                    changed = normalize_text(
                        left_result.response
                    ) != normalize_text(right_result.response)
                    full = left_result.score == 1.0 and right_result.score == 1.0
                    observations.append(
                        _Observation(
                            cluster=clusters[left_id],
                            family=probe_id,
                            values=(
                                float(pair_correct),
                                float(changed),
                                float(pair_correct and changed),
                                float(full and changed),
                            ),
                        )
                    )
        if not observations:
            continue
        metrics: dict[str, JsonValue] = {}
        for index, metric in enumerate(metric_names):
            metrics[metric] = _bootstrap_estimate(
                observations,
                lambda sampled, item=index: _weighted_mean(sampled, item),
                samples=samples,
                confidence_level=confidence_level,
                seed=_derived_seed(seed, f"{label}:counterfactual:{condition}:{metric}"),
            )
        rows.append(
            {
                **_condition_dict(condition),
                **_sample_description(observations),
                "metrics": metrics,
            }
        )
    return rows


def _cross_judge_analysis(
    primary_runs: tuple[ExperimentRun, ...],
    comparison_runs: tuple[ExperimentRun, ...],
    clusters: dict[str, str],
    *,
    samples: int,
    confidence_level: float,
    seed: int,
) -> dict[str, JsonValue]:
    comparison_lookup = {_run_key(run): run for run in comparison_runs}
    by_condition: dict[Condition, list[_Observation]] = {}
    all_observations = []
    for primary in primary_runs:
        comparison = comparison_lookup[_run_key(primary)]
        comparison_results = {
            result.probe_id: result for result in comparison.report.results
        }
        condition = _condition(primary)
        for result in primary.report.results:
            other = comparison_results[result.probe_id]
            observation = _Observation(
                cluster=clusters[primary.profile_id],
                family=result.probe_id,
                values=(result.score, other.score),
                weight=result.weight,
            )
            by_condition.setdefault(condition, []).append(observation)
            all_observations.append(observation)
    metrics = {
        "exact_agreement": _exact_agreement,
        "within_one_grade": _within_one_grade,
        "mean_absolute_error": _mean_absolute_error,
        "mean_difference_comparison_minus_primary": _mean_difference,
        "pearson": _pearson,
        "quadratic_weighted_kappa": _quadratic_weighted_kappa,
    }

    def analyze_group(name: str, observations: list[_Observation]) -> dict[str, JsonValue]:
        return {
            **_sample_description(observations),
            "metrics": {
                metric: _bootstrap_estimate(
                    observations,
                    statistic,
                    samples=samples,
                    confidence_level=confidence_level,
                    seed=_derived_seed(seed, f"cross-judge:{name}:{metric}"),
                )
                for metric, statistic in metrics.items()
            },
        }

    return {
        "design": "same-response paired evaluator comparison",
        "primary_evaluator_ids": sorted(
            {run.report.evaluator_id for run in primary_runs}
        ),
        "comparison_evaluator_ids": sorted(
            {run.report.evaluator_id for run in comparison_runs}
        ),
        "overall": analyze_group("overall", all_observations),
        "by_target_condition": [
            {
                **_condition_dict(condition),
                **analyze_group(str(condition), observations),
            }
            for condition, observations in sorted(by_condition.items())
        ],
    }


def _bootstrap_estimate(
    observations: Sequence[_Observation],
    statistic: Statistic,
    *,
    samples: int,
    confidence_level: float,
    seed: int,
) -> dict[str, JsonValue]:
    if not observations:
        raise StatisticalAnalysisError("Cannot bootstrap an empty observation set.")
    observed = statistic(observations)
    grid: dict[tuple[str, str], list[_Observation]] = {}
    for observation in observations:
        grid.setdefault((observation.cluster, observation.family), []).append(
            observation
        )
    clusters = sorted({observation.cluster for observation in observations})
    families = sorted({observation.family for observation in observations})
    rng = random.Random(seed)
    draws = []
    for _ in range(samples):
        selected_clusters = rng.choices(clusters, k=len(clusters))
        selected_families = rng.choices(families, k=len(families))
        sampled = [
            observation
            for cluster in selected_clusters
            for family in selected_families
            for observation in grid.get((cluster, family), ())
        ]
        value = statistic(sampled)
        if value is not None and math.isfinite(value):
            draws.append(value)
    if observed is None or not math.isfinite(observed):
        return {
            "estimate": None,
            "ci_low": None,
            "ci_high": None,
            "bootstrap_standard_error": None,
            "valid_bootstrap_samples": len(draws),
        }
    if not draws:
        raise StatisticalAnalysisError("Bootstrap statistic was undefined in every draw.")
    alpha = (1.0 - confidence_level) / 2.0
    return {
        "estimate": observed,
        "ci_low": _quantile(draws, alpha),
        "ci_high": _quantile(draws, 1.0 - alpha),
        "bootstrap_standard_error": (
            descriptive_statistics.stdev(draws) if len(draws) > 1 else 0.0
        ),
        "valid_bootstrap_samples": len(draws),
    }


def _score_mean(observations: Sequence[_Observation]) -> float | None:
    return _weighted_mean(observations, 0)


def _weighted_mean(
    observations: Sequence[_Observation], value_index: int
) -> float | None:
    total = sum(observation.weight for observation in observations)
    if not total:
        return None
    return sum(
        observation.values[value_index] * observation.weight
        for observation in observations
    ) / total


def _exact_agreement(observations: Sequence[_Observation]) -> float | None:
    return _weighted_binary(observations, lambda left, right: left == right)


def _within_one_grade(observations: Sequence[_Observation]) -> float | None:
    return _weighted_binary(
        observations, lambda left, right: abs(left - right) <= 0.25 + 1e-12
    )


def _weighted_binary(
    observations: Sequence[_Observation], predicate: Callable[[float, float], bool]
) -> float | None:
    total = sum(observation.weight for observation in observations)
    if not total:
        return None
    return sum(
        float(predicate(observation.values[0], observation.values[1]))
        * observation.weight
        for observation in observations
    ) / total


def _mean_absolute_error(observations: Sequence[_Observation]) -> float | None:
    total = sum(observation.weight for observation in observations)
    if not total:
        return None
    return sum(
        abs(observation.values[0] - observation.values[1]) * observation.weight
        for observation in observations
    ) / total


def _mean_difference(observations: Sequence[_Observation]) -> float | None:
    total = sum(observation.weight for observation in observations)
    if not total:
        return None
    return sum(
        (observation.values[1] - observation.values[0]) * observation.weight
        for observation in observations
    ) / total


def _pearson(observations: Sequence[_Observation]) -> float | None:
    total = sum(observation.weight for observation in observations)
    if not total:
        return None
    left_mean = sum(
        observation.values[0] * observation.weight for observation in observations
    ) / total
    right_mean = sum(
        observation.values[1] * observation.weight for observation in observations
    ) / total
    covariance = sum(
        (observation.values[0] - left_mean)
        * (observation.values[1] - right_mean)
        * observation.weight
        for observation in observations
    )
    left_variance = sum(
        (observation.values[0] - left_mean) ** 2 * observation.weight
        for observation in observations
    )
    right_variance = sum(
        (observation.values[1] - right_mean) ** 2 * observation.weight
        for observation in observations
    )
    if left_variance == 0 or right_variance == 0:
        return None
    return covariance / math.sqrt(left_variance * right_variance)


def _quadratic_weighted_kappa(
    observations: Sequence[_Observation],
) -> float | None:
    total = sum(observation.weight for observation in observations)
    if not total:
        return None
    observed = sum(
        (observation.values[0] - observation.values[1]) ** 2
        * observation.weight
        for observation in observations
    ) / total
    left: dict[float, float] = {}
    right: dict[float, float] = {}
    for observation in observations:
        left[observation.values[0]] = (
            left.get(observation.values[0], 0.0) + observation.weight
        )
        right[observation.values[1]] = (
            right.get(observation.values[1], 0.0) + observation.weight
        )
    expected = sum(
        (left_score - right_score) ** 2
        * left_weight
        / total
        * right_weight
        / total
        for left_score, left_weight in left.items()
        for right_score, right_weight in right.items()
    )
    if expected == 0:
        return 1.0 if observed == 0 else 0.0
    return 1.0 - observed / expected


def _validate_same_response_comparison(
    primary_profiles: dict[str, BenchmarkProfile],
    primary_runs: tuple[ExperimentRun, ...],
    comparison_profiles: dict[str, BenchmarkProfile],
    comparison_runs: tuple[ExperimentRun, ...],
) -> None:
    if primary_profiles != comparison_profiles:
        raise StatisticalAnalysisError(
            "Comparison experiment must use the same frozen benchmark profiles."
        )
    primary_lookup = {_run_key(run): run for run in primary_runs}
    comparison_lookup = {_run_key(run): run for run in comparison_runs}
    if primary_lookup.keys() != comparison_lookup.keys():
        raise StatisticalAnalysisError(
            "Comparison experiment must have the same target conditions and runs."
        )
    for key, primary in primary_lookup.items():
        comparison = comparison_lookup[key]
        primary_results = {
            result.probe_id: result for result in primary.report.results
        }
        comparison_results = {
            result.probe_id: result for result in comparison.report.results
        }
        if primary_results.keys() != comparison_results.keys():
            raise StatisticalAnalysisError(
                f"Comparison probe IDs differ for target run {key}."
            )
        changed = [
            probe_id
            for probe_id in primary_results
            if primary_results[probe_id].response
            != comparison_results[probe_id].response
        ]
        if changed:
            raise StatisticalAnalysisError(
                "Cross-judge analysis requires identical saved responses; changed "
                f"responses in target run {key}: {', '.join(sorted(changed))}."
            )
        incompatible = [
            probe_id
            for probe_id in primary_results
            if (
                primary_results[probe_id].dimension
                != comparison_results[probe_id].dimension
                or primary_results[probe_id].weight
                != comparison_results[probe_id].weight
            )
        ]
        if incompatible:
            raise StatisticalAnalysisError(
                "Comparison result contracts differ in target run "
                f"{key}: {', '.join(sorted(incompatible))}."
            )


def _profiles(plan: ExperimentPlan) -> dict[str, BenchmarkProfile]:
    return {run.profile.profile_id: run.profile for run in plan.runs}


def _identity_clusters(
    profiles: dict[str, BenchmarkProfile],
    pairs: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    clusters: dict[str, str] = {}
    for index, (left, right) in enumerate(pairs, 1):
        cluster = f"pair-{index:04d}:{left}|{right}"
        for profile_id in (left, right):
            if profile_id in clusters:
                raise StatisticalAnalysisError(
                    f"Profile {profile_id!r} belongs to multiple identity clusters."
                )
            clusters[profile_id] = cluster
    for profile_id in profiles:
        clusters.setdefault(profile_id, f"profile:{profile_id}")
    return clusters


def _group_by_condition(
    observations: list[tuple[Condition, _Observation, RunKey]],
) -> dict[Condition, list[_Observation]]:
    grouped: dict[Condition, list[_Observation]] = {}
    for condition, observation, _ in observations:
        grouped.setdefault(condition, []).append(observation)
    return grouped


def _condition(run: ExperimentRun) -> Condition:
    return (run.model, run.reasoning_effort, run.identity_mode)


def _condition_dict(condition: Condition) -> dict[str, JsonValue]:
    model, reasoning_effort, identity_mode = condition
    return {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "identity_mode": identity_mode,
    }


def _run_key(run: ExperimentRun) -> RunKey:
    return (
        run.profile_id,
        run.model,
        run.reasoning_effort,
        run.identity_mode,
        run.repetition,
    )


def _sample_description(observations: Sequence[_Observation]) -> dict[str, JsonValue]:
    return {
        "observations": len(observations),
        "identity_clusters": len({item.cluster for item in observations}),
        "probe_families": len({item.family for item in observations}),
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _derived_seed(seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _validate_configuration(
    *,
    samples: int,
    confidence_level: float,
    comparison_spec: ExperimentSpec | None,
    comparison_report_dir: Path | None,
) -> None:
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 2:
        raise StatisticalAnalysisError(
            "bootstrap samples must be an integer of at least 2"
        )
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, (int, float))
        or not 0.0 < confidence_level < 1.0
    ):
        raise StatisticalAnalysisError("confidence level must be between 0 and 1")
    if (comparison_spec is None) != (comparison_report_dir is None):
        raise StatisticalAnalysisError(
            "comparison experiment and report directory must be supplied together"
        )


def _format_collection(title: str, collection: dict[str, JsonValue]) -> list[str]:
    lines = [
        f"## {title}",
        "",
        f"- Experiment: `{collection['experiment_id']}`",
        f"- Evaluator: {', '.join(str(item) for item in collection['evaluator_ids'])}",
        f"- Runs: {collection['runs']}",
        "",
        "### Target headline scores",
        "",
        "| Model | Reasoning | Mode | Estimate | Confidence interval |",
        "|---|---|---|---:|---:|",
    ]
    for raw in collection["target_conditions"]:
        row = _dict(raw)
        lines.append(
            f"| {row['model']} | {row['reasoning_effort']} | {row['identity_mode']} | "
            f"{_number(row['estimate'])} | {_interval(row)} |"
        )
    lines.extend(
        [
            "",
            "### Paired target differences",
            "",
            "| Contrast | Reasoning | Mode | Estimate | Confidence interval |",
            "|---|---|---|---:|---:|",
        ]
    )
    for raw in collection["paired_target_differences"]:
        row = _dict(raw)
        lines.append(
            f"| {row['right_model']} - {row['left_model']} | "
            f"{row['reasoning_effort']} | {row['identity_mode']} | "
            f"{_number(row['estimate'])} | {_interval(row)} |"
        )
    lines.extend(
        [
            "",
            "### Counterfactual sensitivity",
            "",
            "| Model | Sensitivity | Full-score sensitivity | Response change |",
            "|---|---:|---:|---:|",
        ]
    )
    for raw in collection["counterfactual"]:
        row = _dict(raw)
        metrics = _dict(row["metrics"])
        sensitivity = _dict(metrics["sensitivity"])
        full = _dict(metrics["full_score_sensitivity"])
        changed = _dict(metrics["response_change_rate"])
        lines.append(
            f"| {row['model']} | {_number(sensitivity['estimate'])} "
            f"{_interval(sensitivity)} | {_number(full['estimate'])} "
            f"{_interval(full)} | {_number(changed['estimate'])} "
            f"{_interval(changed)} |"
        )
    lines.append("")
    return lines


def _format_cross_judge(cross: dict[str, JsonValue]) -> list[str]:
    lines = [
        "## Same-response cross-judge agreement",
        "",
        "| Group | Exact | Within one grade | MAE | Mean difference | Pearson | QWK |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    groups = [("overall", _dict(cross["overall"]))]
    groups.extend(
        (str(_dict(item)["model"]), _dict(item))
        for item in cross["by_target_condition"]
    )
    keys = (
        "exact_agreement",
        "within_one_grade",
        "mean_absolute_error",
        "mean_difference_comparison_minus_primary",
        "pearson",
        "quadratic_weighted_kappa",
    )
    for name, group in groups:
        metrics = _dict(group["metrics"])
        cells = []
        for key in keys:
            metric = _dict(metrics[key])
            cells.append(f"{_number(metric['estimate'])} {_interval(metric)}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _interval(row: dict[str, JsonValue]) -> str:
    return f"[{_number(row['ci_low'])}, {_number(row['ci_high'])}]"


def _number(value: JsonValue) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StatisticalAnalysisError("Expected a numeric report value.")
    return f"{float(value):.3f}"


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise StatisticalAnalysisError("Expected an object in statistical report.")
    return value


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
