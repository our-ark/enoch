from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from identity_benchmark.adapters import AgentAdapter, InstanceError
from identity_benchmark.contracts import (
    BenchmarkProfile,
    BenchmarkReport,
    BenchmarkRequest,
    ExpectationResult,
    Probe,
    ProbeResult,
)
from identity_benchmark.evaluators import (
    DeterministicEvaluator,
    EvaluationRequest,
    Evaluator,
    EvaluatorError,
)
from identity_benchmark.scoring import (
    DeterministicScorer,
    ExpectationScorer,
    weighted_expectation_score,
)


METRIC_DIMENSIONS = {
    "identity_recall": {"recognition", "separation"},
    "behavioral_consistency": {"application", "consistency"},
    "conflict_resistance": {"resistance"},
    "longitudinal_stability": {"retention"},
    "update_governance": {"governance"},
}
METRIC_TAGS = {
    "lineage_consistency": {"family-lineage"},
    "relationship_consistency": {"relationship"},
    "relationship_addressing": {"relationship-addressing"},
    "mission_consistency": {"mission"},
    "safe_identity_enactment": {"safety-boundary"},
    "safety_compliance": {"safety-compliance"},
    "mission_recovery": {"mission-recovery"},
    "source_nonidentification": {"source-leakage"},
    "identity_layering": {"identity-layering"},
    "persistent_update": {"persistent-update"},
    "implicit_enactment": {"implicit-behavior"},
    "open_generation": {"open-generation"},
    "compositional_reasoning": {"compositional-reasoning"},
    "adversarial_context": {"adversarial-context"},
    "rollback_integrity": {"rollback-integrity"},
}
HEADLINE_EXCLUDED_TAGS = {"safety-boundary"}


def run_benchmark(
    profile: BenchmarkProfile,
    instance: AgentAdapter,
    scorer: ExpectationScorer | None = None,
    evaluator: Evaluator | None = None,
) -> BenchmarkReport:
    """Run every profile probe in an isolated target session."""
    if scorer is not None and evaluator is not None:
        raise ValueError("use either scorer or evaluator, not both")
    active_evaluator = evaluator or DeterministicEvaluator(
        scorer=scorer or DeterministicScorer()
    )
    started_at = _now()
    results: list[ProbeResult] = []
    for probe in profile.probes:
        request = BenchmarkRequest(
            profile_id=profile.profile_id,
            probe_id=probe.id,
            messages=probe.messages,
            after_response=probe.after_response,
        )
        try:
            response = instance.respond(request)
        except InstanceError as error:
            results.append(
                ProbeResult(
                    probe_id=probe.id,
                    dimension=probe.dimension,
                    score=0.0,
                    weight=probe.weight,
                    response="",
                    expectations=(),
                    error=str(error),
                )
            )
            continue
        try:
            identity_probe = replace(
                probe,
                expectations=tuple(
                    expectation
                    for expectation in probe.expectations
                    if expectation.aspect == "identity"
                ),
                after_response=None,
                reference_statements=(),
            )
            evaluation = active_evaluator.evaluate(
                EvaluationRequest(
                    profile_id=profile.profile_id,
                    statements=(
                        probe.reference_statements or profile.statements
                    ),
                    probe=identity_probe,
                    agent_response=response.response,
                )
            )
        except EvaluatorError as error:
            results.append(
                ProbeResult(
                    probe_id=probe.id,
                    dimension=probe.dimension,
                    score=0.0,
                    weight=probe.weight,
                    response=response.response,
                    expectations=(),
                    metadata=response.metadata,
                    error=f"evaluation failed: {error}",
                )
            )
            continue
        secondary_results, component_scores = _secondary_components(
            probe,
            response.response,
        )
        component_scores["identity"] = evaluation.score
        results.append(
            ProbeResult(
                probe_id=probe.id,
                dimension=probe.dimension,
                score=evaluation.score,
                weight=probe.weight,
                response=response.response,
                expectations=evaluation.expectation_results + secondary_results,
                metadata=response.metadata,
                evaluation_metadata=evaluation.metadata,
                component_scores=component_scores,
            )
        )
    materialized = tuple(results)
    return BenchmarkReport(
        profile_id=profile.profile_id,
        instance_id=instance.instance_id,
        started_at=started_at,
        finished_at=_now(),
        evaluator_id=active_evaluator.evaluator_id,
        score=_headline_score(profile, materialized),
        dimension_scores=_dimension_scores(materialized),
        metric_scores=_metric_scores(profile, materialized),
        results=materialized,
    )


def _weighted_score(results: tuple[ProbeResult, ...]) -> float:
    total = sum(result.weight for result in results)
    if total == 0:
        return 0.0
    return sum(result.score * result.weight for result in results) / total


def _headline_score(
    profile: BenchmarkProfile,
    results: tuple[ProbeResult, ...],
) -> float:
    tags_by_probe = {probe.id: set(probe.tags) for probe in profile.probes}
    selected = tuple(
        result
        for result in results
        if result.dimension != "capability"
        and not HEADLINE_EXCLUDED_TAGS.intersection(
            tags_by_probe.get(result.probe_id, set())
        )
    )
    return _weighted_score(selected)


def _dimension_scores(results: tuple[ProbeResult, ...]) -> dict[str, float]:
    dimensions = sorted({result.dimension for result in results})
    return {
        dimension: _weighted_score(
            tuple(result for result in results if result.dimension == dimension)
        )
        for dimension in dimensions
    }


def _metric_scores(
    profile: BenchmarkProfile,
    results: tuple[ProbeResult, ...],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for metric, dimensions in METRIC_DIMENSIONS.items():
        selected = tuple(
            result for result in results if result.dimension in dimensions
        )
        if selected:
            scores[metric] = _weighted_score(selected)
    probe_tags = {probe.id: set(probe.tags) for probe in profile.probes}
    for metric, required_tags in METRIC_TAGS.items():
        selected = tuple(
            result
            for result in results
            if required_tags.issubset(probe_tags.get(result.probe_id, set()))
        )
        if selected:
            scores[metric] = _weighted_score(selected)
    component_names = sorted(
        {
            component
            for result in results
            for component in result.component_scores
            if component != "identity"
        }
    )
    for component in component_names:
        selected = tuple(
            result for result in results if component in result.component_scores
        )
        total = sum(result.weight for result in selected)
        if total:
            scores[f"{component}_compliance"] = sum(
                result.component_scores[component] * result.weight
                for result in selected
            ) / total
            if component == "constraint":
                scores["constraint_identity_agreement"] = sum(
                    (
                        1.0
                        - abs(
                            result.score
                            - result.component_scores[component]
                        )
                    )
                    * result.weight
                    for result in selected
                ) / total
    return scores


def _secondary_components(
    probe: Probe,
    response: str,
) -> tuple[tuple[ExpectationResult, ...], dict[str, float]]:
    scorer = DeterministicScorer()
    results = []
    component_scores: dict[str, float] = {}
    aspects = sorted(
        {expectation.aspect for expectation in probe.expectations}
        - {"identity"}
    )
    for aspect in aspects:
        aspect_results = tuple(
            scorer.score(response, expectation)
            for expectation in probe.expectations
            if expectation.aspect == aspect
        )
        results.extend(aspect_results)
        component_scores[aspect] = weighted_expectation_score(aspect_results)
    return tuple(results), component_scores


def _now() -> str:
    return datetime.now(UTC).isoformat()
