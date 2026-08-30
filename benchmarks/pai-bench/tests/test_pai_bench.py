from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from identity_benchmark.contracts import (
    BenchmarkRequest,
    InstanceResponse,
    load_benchmark_profile,
)
from identity_benchmark.experiments import load_experiment_spec
from identity_benchmark.pai_bench import (
    PROBES_PER_PROFILE,
    RELEASE_VERSION,
    SOURCE_SEED,
    SPLIT_PAIR_NUMBERS,
)
from identity_benchmark.runner import run_benchmark
from identity_benchmark.probe_suites import (
    compile_benchmark_profile,
    load_identity_profile,
    load_probe_bindings,
    load_probe_suite,
)


SUITE = ROOT / "releases" / "v1.0" / "data"
INDEX = SUITE / "population.json"
PROTOCOL = SUITE / "protocol.json"
DECOUPLED_INDEX = SUITE / "decoupled-index.json"
PROBE_SUITE = SUITE / "probe-suite.json"
VERSION = ROOT / "VERSION"


class PaiBenchTests(unittest.TestCase):
    def test_public_release_version_is_explicit(self) -> None:
        self.assertEqual(
            VERSION.read_text(encoding="utf-8").strip(),
            RELEASE_VERSION,
        )

    def test_decoupled_sources_compile_to_the_frozen_profile_snapshots(self) -> None:
        index = _load(DECOUPLED_INDEX)
        suite = load_probe_suite(PROBE_SUITE)

        self.assertEqual(len(suite.probes), PROBES_PER_PROFILE)
        self.assertEqual(len(index["profiles"]), 24)
        for entry in index["profiles"]:
            identity = load_identity_profile(SUITE / entry["identity_path"])
            bindings = load_probe_bindings(SUITE / entry["bindings_path"])
            compiled = compile_benchmark_profile(identity, suite, bindings)
            frozen = load_benchmark_profile(SUITE / entry["path"])

            self.assertEqual(compiled.to_dict(), frozen.to_dict())

    def test_decoupled_manifests_use_one_shared_probe_suite(self) -> None:
        for filename in (
            "dev-decoupled-experiment.json",
            "test-decoupled-experiment.json",
            "source-challenge-decoupled-experiment.json",
        ):
            spec = load_experiment_spec(SUITE / filename)

            self.assertEqual(spec.probe_suite_path, PROBE_SUITE)
            self.assertEqual(len(spec.profile_paths), 8)
            self.assertEqual(len(spec.probe_binding_paths), 8)

    def test_frozen_release_is_current_and_valid(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "build_release.py"),
                "--check",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        index = _load(INDEX)
        profiles = tuple(
            load_benchmark_profile(SUITE / entry["path"])
            for entry in index["profiles"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(index["generator_version"], "identity-publication-v4.1")
        self.assertEqual(index["seed"], SOURCE_SEED)
        self.assertEqual(index["population_size"], 24)
        self.assertEqual(index["probes_per_profile"], PROBES_PER_PROFILE)
        self.assertEqual(index["strata"], {
            "factorial": 16,
            "source-inspired-composite": 8,
        })
        self.assertEqual(len(index["counterfactual_pairs"]), 12)
        for profile in profiles:
            assert profile.agent_identity is not None
            self.assertEqual(profile.agent_identity.get("schema_version"), 1)
            self.assertEqual(len(profile.probes), PROBES_PER_PROFILE)
            self.assertEqual(
                sum(probe.after_response is not None for probe in profile.probes),
                2,
            )
            self.assertEqual(
                sum("counterfactual" in probe.tags for probe in profile.probes),
                7,
            )
            for probe in profile.probes:
                if probe.after_response is not None:
                    self.assertEqual(
                        probe.after_response.agent_identity.get("schema_version"),
                        1,
                    )
                    self.assertEqual(
                        probe.after_response.agent_identity["identity"]["id"],
                        profile.profile_id,
                    )

    def test_dev_test_and_source_challenge_are_disjoint_and_frozen(self) -> None:
        index = _load(INDEX)
        protocol = _load(PROTOCOL)
        entries = {entry["profile_id"]: entry for entry in index["profiles"]}
        observed: set[str] = set()

        self.assertEqual(protocol["source_seed"], SOURCE_SEED)
        self.assertTrue(protocol["test_policy"]["configuration_locked_before_test"])
        self.assertEqual(
            protocol["test_policy"]["locked_generator_version"],
            "identity-publication-v4.1",
        )
        self.assertFalse(
            protocol["test_policy"]["test_responses_observed_before_lock"]
        )
        self.assertTrue(protocol["test_policy"]["report_all_frozen_test_runs"])
        self.assertFalse(protocol["test_policy"]["tune_prompts_or_rubric_on_test"])
        self.assertEqual(
            protocol["dev_revision_history"],
            [
                {
                    "generator_version": "identity-publication-v4.1",
                    "change": (
                        "Score open mission recovery continuously instead of using "
                        "the complete mission sentence as a binary semantic gate."
                    ),
                    "evidence_scope": "development split only",
                    "test_or_source_challenge_responses_observed": False,
                }
            ],
        )
        for split, pair_numbers in SPLIT_PAIR_NUMBERS.items():
            split_spec = protocol["splits"][split]
            profile_ids = set(split_spec["profile_ids"])
            self.assertEqual(split_spec["pair_numbers"], list(pair_numbers))
            self.assertFalse(observed.intersection(profile_ids))
            observed.update(profile_ids)
            self.assertEqual(len(profile_ids), 8)
            self.assertEqual(
                {entries[profile_id]["category"] for profile_id in profile_ids},
                {"prosocial", "neutral", "antagonistic", "conflicted"},
            )
            self.assertEqual(split_spec["frozen"], split != "dev")
            self.assertEqual(split_spec["tuning_allowed"], split == "dev")
        self.assertEqual(observed, set(entries))

        for filename, expected_profiles in (
            ("dev-experiment.json", 8),
            ("test-experiment.json", 8),
            ("source-challenge-experiment.json", 8),
        ):
            spec = load_experiment_spec(SUITE / filename)
            self.assertEqual(len(spec.profile_paths), expected_profiles)
            self.assertEqual(len(spec.counterfactual_pairs), 4)
            assert spec.evaluator is not None
            self.assertEqual(spec.evaluator.model, "gpt-5.6-sol")
            self.assertEqual(spec.evaluator.reasoning_effort, "xhigh")

    def test_atomic_metrics_have_redundancy_and_do_not_recombine_safety(self) -> None:
        profile = _first_profile()
        tags = {
            tag: [probe for probe in profile.probes if tag in probe.tags]
            for tag in (
                "safety-compliance",
                "mission-recovery",
                "relationship-addressing",
                "family-lineage",
                "rollback-integrity",
            )
        }

        self.assertEqual(len(tags["safety-compliance"]), 3)
        self.assertEqual(len(tags["mission-recovery"]), 3)
        self.assertGreaterEqual(len(tags["relationship-addressing"]), 3)
        self.assertGreaterEqual(len(tags["family-lineage"]), 3)
        self.assertGreaterEqual(len(tags["rollback-integrity"]), 6)
        self.assertTrue(
            all(
                "mission-recovery" not in probe.tags
                and "relationship-addressing" not in probe.tags
                for probe in tags["safety-compliance"]
            )
        )
        self.assertTrue(
            all("safety-compliance" not in probe.tags for probe in tags["mission-recovery"])
        )
        self.assertTrue(
            all(
                not any(
                    expectation.gate and expectation.aspect == "identity"
                    for expectation in probe.expectations
                )
                for probe in tags["mission-recovery"]
            )
        )
        rollback_ids = {probe.id for probe in tags["rollback-integrity"]}
        self.assertTrue(
            {
                "rollback-designation-atomic",
                "rollback-parent-atomic",
                "rollback-address-atomic",
                "rollback-mission-atomic",
                "rollback-behavior-atomic",
                "rollback-composition",
            }.issubset(rollback_ids)
        )

    def test_counterfactual_oracles_change_only_when_identity_requires_it(self) -> None:
        index = _load(INDEX)
        paths = {
            entry["profile_id"]: SUITE / entry["path"]
            for entry in index["profiles"]
        }
        for left_id, right_id in index["counterfactual_pairs"]:
            left = load_benchmark_profile(paths[left_id])
            right = load_benchmark_profile(paths[right_id])
            left_probes = {probe.id: probe for probe in left.probes}
            right_probes = {probe.id: probe for probe in right.probes}
            paired_ids = {
                probe.id for probe in left.probes if "counterfactual" in probe.tags
            }

            self.assertEqual(len(paired_ids), 7)
            for probe_id in paired_ids:
                self.assertEqual(
                    left_probes[probe_id].messages,
                    right_probes[probe_id].messages,
                )
                self.assertNotEqual(
                    left_probes[probe_id].expectations,
                    right_probes[probe_id].expectations,
                )
                self.assertNotEqual(
                    _oracle_answer(left_probes[probe_id]),
                    _oracle_answer(right_probes[probe_id]),
                )

    def test_positive_oracle_and_identity_blind_negative_control(self) -> None:
        index = _load(INDEX)
        for entry in index["profiles"]:
            profile = load_benchmark_profile(SUITE / entry["path"])
            oracle_report = run_benchmark(profile, _ExpectationOracle(profile))
            negative_report = run_benchmark(profile, _IdentityBlindAgent())

            self.assertEqual(oracle_report.score, 1.0, profile.profile_id)
            self.assertEqual(
                oracle_report.metric_scores["safety_compliance"],
                1.0,
                profile.profile_id,
            )
            self.assertEqual(
                oracle_report.metric_scores["mission_recovery"],
                1.0,
                profile.profile_id,
            )
            self.assertLess(negative_report.score, 0.2, profile.profile_id)

    def test_source_challenge_profiles_do_not_reveal_prototypes(self) -> None:
        index = _load(INDEX)
        catalog = _load(SUITE / "source-prototypes.json")
        forbidden = {
            value.casefold()
            for source in catalog["sources"]
            for value in (source["id"], source["name"], str(source["death_year"]))
        }
        composites = [
            entry
            for entry in index["profiles"]
            if entry["stratum"] == "source-inspired-composite"
        ]

        self.assertEqual(len(composites), 8)
        for entry in composites:
            profile_text = json.dumps(
                _load(SUITE / entry["path"]), ensure_ascii=False
            ).casefold()
            self.assertTrue(all(value not in profile_text for value in forbidden))


class _ExpectationOracle:
    instance_id = "publication-v4-oracle"

    def __init__(self, profile) -> None:
        self.answers = {
            probe.id: _oracle_answer(probe) for probe in profile.probes
        }

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        return InstanceResponse(response=self.answers[request.probe_id])


class _IdentityBlindAgent:
    instance_id = "publication-v4-negative-control"

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        del request
        return InstanceResponse(response="I will handle this thoughtfully.")


def _oracle_answer(probe) -> str:
    identity_expectations = [
        expectation
        for expectation in probe.expectations
        if expectation.aspect == "identity"
    ]
    exact = [
        expectation.value
        for expectation in identity_expectations
        if expectation.type == "exact"
    ]
    if exact:
        return exact[0]
    contains = [
        expectation.value
        for expectation in identity_expectations
        if expectation.type == "contains"
    ]
    headings = [
        value
        for value in contains
        if value.startswith("Decision: ") or value.startswith("Recommendation: ")
    ]
    remainder = [value for value in contains if value not in headings]
    return "\n".join(headings + remainder) or "identity-oracle"


def _first_profile():
    index = _load(INDEX)
    return load_benchmark_profile(SUITE / index["profiles"][0]["path"])


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
