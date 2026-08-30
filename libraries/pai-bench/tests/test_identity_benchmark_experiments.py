from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REPO_ROOT = ROOT.parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

from identity_benchmark.experiments import (
    ExperimentError,
    format_experiment_plan,
    format_experiment_report,
    load_experiment_spec,
    plan_experiment,
    run_experiment,
)


PROFILE = FIXTURES / "synthetic-profile.json"
INSTANCE = FIXTURES / "synthetic-instance.py"
VECTOR_NORTH = FIXTURES / "counterfactual" / "vector-north.json"
VECTOR_SOUTH = VECTOR_NORTH.with_name("vector-south.json")


class IdentityBenchmarkExperimentTests(unittest.TestCase):
    def _manifest(self, directory: Path, *, repetitions: int = 1) -> Path:
        manifest = directory / "experiment.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "experiment_id": "resumable-fixture",
                    "profile": str(PROFILE),
                    "body_root": str(ROOT),
                    "instance_command": [sys.executable, str(INSTANCE)],
                    "models": ["model-a"],
                    "reasoning_efforts": ["medium"],
                    "identity_modes": ["none", "full-context"],
                    "repetitions": repetitions,
                    "timeout_seconds": 10,
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_matrix_uses_fresh_state_per_condition_and_aggregates_scores(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            manifest = temporary / "experiment.json"
            output = temporary / "reports"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_id": "fixture-matrix",
                        "profile": str(PROFILE),
                        "body_root": str(ROOT),
                        "instance_command": [sys.executable, str(INSTANCE)],
                        "models": ["model-a"],
                        "reasoning_efforts": ["medium"],
                        "identity_modes": ["none", "full-context"],
                        "repetitions": 1,
                        "timeout_seconds": 10,
                    }
                ),
                encoding="utf-8",
            )

            spec = load_experiment_spec(manifest)
            report = run_experiment(spec, output)
            saved = json.loads(
                (output / "experiment-report.json").read_text(encoding="utf-8")
            )
            state_homes = {
                result.metadata["state_home"]
                for run in report.runs
                for result in run.report.results
            }

        self.assertEqual(len(report.runs), 2)
        self.assertEqual({run.report.score for run in report.runs}, {1.0})
        self.assertEqual(len(report.aggregates), 2)
        self.assertEqual(report.aggregates[0]["score_stddev"], 0.0)
        self.assertEqual(
            set(report.aggregates[0]["mean_metric_scores"].values()), {1.0}
        )
        self.assertEqual(report.identity_gains[0]["gain"], 0.0)
        self.assertEqual(len(state_homes), 2)
        self.assertTrue(all(not Path(path).exists() for path in state_homes))
        self.assertEqual(saved["experiment_id"], "fixture-matrix")
        self.assertIn("Identity gain", format_experiment_report(report))
        self.assertIn("behavioral_consistency", format_experiment_report(report))

    def test_bundled_local_manifest_resolves_to_the_library_root(self) -> None:
        manifest = FIXTURES / "local-smoke-experiment.json"

        spec = load_experiment_spec(manifest)

        self.assertEqual(spec.body_root, ROOT)
        self.assertEqual(spec.profile_path, PROFILE)
        self.assertEqual(spec.identity_modes, ("none", "full-context"))

    def test_multi_profile_matrix_reports_counterfactual_sensitivity(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            manifest = temporary / "experiment.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_id": "counterfactual-fixture",
                        "profiles": [str(VECTOR_NORTH), str(VECTOR_SOUTH)],
                        "body_root": str(ROOT),
                        "instance_command": [sys.executable, str(INSTANCE)],
                        "models": ["model-a"],
                        "reasoning_efforts": ["low"],
                        "identity_modes": ["none", "full-context"],
                        "counterfactual_pairs": [
                            ["vector-north-v2", "vector-south-v2"]
                        ],
                        "timeout_seconds": 10,
                    }
                ),
                encoding="utf-8",
            )

            spec = load_experiment_spec(manifest)
            report = run_experiment(spec, temporary / "reports")

        self.assertEqual(spec.profile_paths, (VECTOR_NORTH, VECTOR_SOUTH))
        self.assertEqual(report.profile_ids, ("vector-north-v2", "vector-south-v2"))
        self.assertEqual(len(report.runs), 4)
        self.assertEqual(len(report.aggregates), 4)
        self.assertEqual(len(report.counterfactual_metrics), 2)
        metrics = {
            metric["identity_mode"]: metric
            for metric in report.counterfactual_metrics
        }
        self.assertEqual(metrics["none"]["probe_pairs"], 9)
        self.assertEqual(metrics["none"]["sensitivity"], 0.0)
        self.assertEqual(metrics["full-context"]["probe_pairs"], 9)
        self.assertEqual(metrics["full-context"]["paired_accuracy"], 1.0)
        self.assertEqual(metrics["full-context"]["response_change_rate"], 1.0)
        self.assertEqual(metrics["full-context"]["sensitivity"], 1.0)
        self.assertIn("Counterfactual sensitivity", format_experiment_report(report))

    def test_counterfactual_pair_requires_opposing_expectations(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            duplicate = temporary / "duplicate.json"
            value = json.loads(VECTOR_NORTH.read_text(encoding="utf-8"))
            value["profile_id"] = "vector-duplicate-v2"
            duplicate.write_text(json.dumps(value), encoding="utf-8")
            manifest = temporary / "experiment.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_id": "invalid-counterfactual",
                        "profiles": [str(VECTOR_NORTH), str(duplicate)],
                        "body_root": str(ROOT),
                        "instance_command": [sys.executable, str(INSTANCE)],
                        "models": ["model-a"],
                        "reasoning_efforts": ["low"],
                        "identity_modes": ["none"],
                        "counterfactual_pairs": [
                            ["vector-north-v2", "vector-duplicate-v2"]
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ExperimentError, "different expectations"):
                run_experiment(load_experiment_spec(manifest), temporary / "reports")

    def test_counterfactual_accuracy_uses_gated_outcome_not_total_score(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            profiles = []
            pair = []
            for source in (VECTOR_NORTH, VECTOR_SOUTH):
                value = json.loads(source.read_text(encoding="utf-8"))
                pair.append(value["profile_id"])
                for probe in value["probes"]:
                    if "counterfactual" not in probe["tags"]:
                        continue
                    probe["expectations"].append(
                        {
                            "type": "contains",
                            "value": "IMPOSSIBLE-NONGATED-RATIONALE",
                            "gate": False,
                        }
                    )
                path = temporary / source.name
                path.write_text(json.dumps(value), encoding="utf-8")
                profiles.append(str(path))
            manifest = temporary / "experiment.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_id": "counterfactual-partial-rationale",
                        "profiles": profiles,
                        "body_root": str(ROOT),
                        "instance_command": [sys.executable, str(INSTANCE)],
                        "models": ["model-a"],
                        "reasoning_efforts": ["low"],
                        "identity_modes": ["full-context"],
                        "counterfactual_pairs": [pair],
                        "timeout_seconds": 10,
                    }
                ),
                encoding="utf-8",
            )

            report = run_experiment(
                load_experiment_spec(manifest), temporary / "reports"
            )

        metric = report.counterfactual_metrics[0]
        self.assertEqual(metric["paired_accuracy"], 1.0)
        self.assertEqual(metric["sensitivity"], 1.0)
        self.assertLess(metric["paired_full_score_rate"], 1.0)
        self.assertLess(metric["full_score_sensitivity"], 1.0)

    def test_plan_partitions_atomic_conditions_deterministically(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec = load_experiment_spec(self._manifest(temporary, repetitions=2))

            first = plan_experiment(spec, batch_size=3, batch_index=1)
            second = plan_experiment(spec, batch_size=3, batch_index=2)

        self.assertEqual(first.total_runs, 4)
        self.assertEqual(first.total_batches, 2)
        self.assertEqual(
            [run.run_id for run in first.selected_runs],
            ["run-0001", "run-0002", "run-0003"],
        )
        self.assertEqual(
            [run.run_id for run in second.selected_runs], ["run-0004"]
        )
        self.assertEqual(first.campaign_fingerprint, second.campaign_fingerprint)
        self.assertEqual(first.runs[0].fingerprint, second.runs[0].fingerprint)
        self.assertIn("Batch: 2/2", format_experiment_plan(second))

    def test_batches_accumulate_and_resume_skips_completed_runs(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec = load_experiment_spec(self._manifest(temporary))
            output = temporary / "reports"

            first = run_experiment(spec, output, batch_size=1, batch_index=1)
            run_one_path = output / "runs" / "run-0001.json"
            run_one = run_one_path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ExperimentError, "--resume"):
                run_experiment(spec, output, batch_size=1, batch_index=2)

            second = run_experiment(
                spec,
                output,
                batch_size=1,
                batch_index=2,
                resume=True,
            )
            rerun = run_experiment(
                spec,
                output,
                batch_size=1,
                batch_index=1,
                resume=True,
            )

            saved = json.loads(
                (output / "experiment-report.json").read_text(encoding="utf-8")
            )
            run_one_after_resume = run_one_path.read_text(encoding="utf-8")

        self.assertFalse(first.is_complete)
        self.assertEqual(first.completed_runs, 1)
        self.assertTrue(second.is_complete)
        self.assertEqual(second.completed_runs, 2)
        self.assertTrue(rerun.is_complete)
        self.assertEqual(run_one, run_one_after_resume)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["progress"]["completed_runs"], 2)

    def test_resume_rejects_a_tampered_run_fingerprint(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec = load_experiment_spec(self._manifest(temporary))
            output = temporary / "reports"
            run_experiment(spec, output, batch_size=1)
            path = output / "runs" / "run-0001.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["fingerprint"] = "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(ExperimentError, "fingerprint"):
                run_experiment(spec, output, batch_size=1, resume=True)

    def test_resume_retries_a_saved_run_with_probe_errors(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec = load_experiment_spec(self._manifest(temporary))
            output = temporary / "reports"
            run_experiment(spec, output, batch_size=1)
            path = output / "runs" / "run-0001.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["report"]["results"][0]["error"] = "forced interruption"
            value["report"]["errors"] = 1
            path.write_text(json.dumps(value), encoding="utf-8")

            resumed = run_experiment(spec, output, batch_size=1, resume=True)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(resumed.completed_runs, 1)
        self.assertEqual(saved["report"]["errors"], 0)
        self.assertEqual(saved["report"]["results"][0]["error"], "")

    def test_resume_rejects_changed_experiment_inputs(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            manifest = self._manifest(temporary)
            output = temporary / "reports"
            run_experiment(load_experiment_spec(manifest), output, batch_size=1)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["models"] = ["model-b"]
            manifest.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(ExperimentError, "changed"):
                run_experiment(
                    load_experiment_spec(manifest),
                    output,
                    batch_size=1,
                    resume=True,
                )

    def test_public_release_manifests_keep_the_documented_batch_counts(self) -> None:
        cases = (
            ("dev-decoupled-experiment.json", 4, 32, 8),
            ("test-decoupled-experiment.json", 4, 24, 6),
            ("source-challenge-decoupled-experiment.json", 4, 24, 6),
        )
        release = REPO_ROOT / "benchmarks" / "pai-bench" / "v1.0"
        for relative, batch_size, total_runs, total_batches in cases:
            with self.subTest(task=relative):
                plan = plan_experiment(
                    load_experiment_spec(release / relative),
                    batch_size=batch_size,
                )
                self.assertEqual(plan.total_runs, total_runs)
                self.assertEqual(plan.total_batches, total_batches)


if __name__ == "__main__":
    unittest.main()
