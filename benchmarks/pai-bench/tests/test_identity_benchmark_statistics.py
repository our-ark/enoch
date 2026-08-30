from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "tests" / "fixtures"

from identity_benchmark.experiments import load_experiment_spec, run_experiment
from identity_benchmark.statistics import (
    StatisticalAnalysisError,
    analyze_experiment,
    format_statistical_analysis,
    write_statistical_analysis,
)


INSTANCE = FIXTURES / "synthetic-instance.py"
VECTOR_NORTH = FIXTURES / "counterfactual" / "vector-north.json"
VECTOR_SOUTH = VECTOR_NORTH.with_name("vector-south.json")


class IdentityBenchmarkStatisticsTests(unittest.TestCase):
    def _experiment(self, directory: Path):
        manifest = directory / "experiment.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "experiment_id": "bootstrap-fixture",
                    "profiles": [str(VECTOR_NORTH), str(VECTOR_SOUTH)],
                    "body_root": str(ROOT),
                    "instance_command": [sys.executable, str(INSTANCE)],
                    "models": ["model-a", "model-b"],
                    "reasoning_efforts": ["low"],
                    "identity_modes": ["full-context"],
                    "counterfactual_pairs": [
                        ["vector-north-v2", "vector-south-v2"]
                    ],
                    "timeout_seconds": 10,
                }
            ),
            encoding="utf-8",
        )
        spec = load_experiment_spec(manifest)
        output = directory / "runs"
        run_experiment(spec, output)
        return spec, output

    def test_crossed_bootstrap_is_reproducible_and_preserves_pairing(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec, output = self._experiment(temporary)

            first = analyze_experiment(
                spec,
                output,
                comparison_spec=spec,
                comparison_report_dir=output,
                samples=100,
                seed=17,
            )
            second = analyze_experiment(
                spec,
                output,
                comparison_spec=spec,
                comparison_report_dir=output,
                samples=100,
                seed=17,
            )
            json_path, markdown_path = write_statistical_analysis(
                temporary / "analysis", first
            )

        self.assertEqual(first, second)
        primary = first["primary"]
        self.assertEqual(primary["identity_clusters"], 1)
        self.assertEqual(len(primary["target_conditions"]), 2)
        difference = primary["paired_target_differences"][0]
        self.assertEqual(difference["estimate"], 0.0)
        self.assertEqual(difference["ci_low"], 0.0)
        self.assertEqual(difference["ci_high"], 0.0)
        exact = first["cross_judge"]["overall"]["metrics"]["exact_agreement"]
        self.assertEqual(exact["estimate"], 1.0)
        self.assertEqual(exact["ci_low"], 1.0)
        self.assertEqual(exact["ci_high"], 1.0)
        self.assertTrue(json_path.name.endswith(".json"))
        self.assertTrue(markdown_path.name.endswith(".md"))
        rendered = format_statistical_analysis(first)
        self.assertIn("Paired target differences", rendered)
        self.assertIn("Same-response cross-judge agreement", rendered)

    def test_cross_judge_analysis_rejects_changed_target_responses(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec, output = self._experiment(temporary)
            changed = temporary / "changed"
            shutil.copytree(output, changed)
            run_path = changed / "runs" / "run-0001.json"
            value = json.loads(run_path.read_text(encoding="utf-8"))
            value["report"]["results"][0]["response"] += " changed"
            run_path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(
                StatisticalAnalysisError, "identical saved responses"
            ):
                analyze_experiment(
                    spec,
                    output,
                    comparison_spec=spec,
                    comparison_report_dir=changed,
                    samples=20,
                )

    def test_invalid_bootstrap_configuration_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            spec, output = self._experiment(temporary)

            with self.assertRaisesRegex(
                StatisticalAnalysisError, "at least 2"
            ):
                analyze_experiment(spec, output, samples=1)
            with self.assertRaisesRegex(
                StatisticalAnalysisError, "supplied together"
            ):
                analyze_experiment(
                    spec,
                    output,
                    comparison_report_dir=output,
                    samples=20,
                )


if __name__ == "__main__":
    unittest.main()
