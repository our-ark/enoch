from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.evolve import (
    MODE_DISABLED,
    collect_evolve_candidates,
    evolve_report,
    load_evolve_state,
    rank_evolve_candidates,
    set_evolve_mode,
    set_evolve_theme,
)
from enoch.lineage.core import LineageCandidate


class EnochEvolveTests(unittest.TestCase):
    def test_default_state_is_co_evolve(self) -> None:
        with TemporaryDirectory() as temp:
            state = load_evolve_state(Path(temp))

        self.assertEqual(state.mode, "co-evolve")
        self.assertEqual(state.theme, "")

    def test_collects_backlog_and_parent_inheritance_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            _write_lineage_candidate(root, _lineage_candidate())

            candidates = collect_evolve_candidates(root)
            ranked = rank_evolve_candidates(candidates, theme="Telegram UX")

        self.assertEqual({candidate.source for candidate in candidates}, {"backlog", "inheritance"})
        self.assertEqual(ranked[0].source, "backlog")
        self.assertIn("Telegram", ranked[0].title)

    def test_disabled_mode_does_not_collect_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "do this later", root, priority="p0")
            set_evolve_mode(MODE_DISABLED, root)

            report = evolve_report(root)

        self.assertEqual(report.state.mode, MODE_DISABLED)
        self.assertEqual(report.candidates, ())
        self.assertIsNone(report.top_candidate)

    def test_theme_is_persisted(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            state = set_evolve_theme(" improve Telegram work UX ", root)
            loaded = load_evolve_state(root)

        self.assertEqual(state.theme, "improve Telegram work UX")
        self.assertEqual(loaded.theme, "improve Telegram work UX")


def _write_lineage_candidate(root: Path, candidate: LineageCandidate) -> None:
    lineage = root / ".agent" / "lineage.yaml"
    lineage.parent.mkdir(parents=True)
    lineage.write_text("parent:\n  name: Seth\n  repo: our-ark/enoch\n", encoding="utf-8")
    inbox = root / ".agent" / "lineage_inbox.json"
    inbox.write_text(json.dumps({"schema_version": 1, "candidates": [candidate.__dict__]}), encoding="utf-8")


def _lineage_candidate() -> LineageCandidate:
    return LineageCandidate(
        id="our-ark/enoch#32",
        repo="our-ark/enoch",
        pr_number=32,
        title="Add Telegram recovery command",
        url="https://github.com/our-ark/enoch/pull/32",
        merged_at="2026-06-17T01:31:12Z",
        merge_commit="abc123",
        ancestor_name="Seth",
        depth=1,
        labels=("inherit:recommended",),
        files=("src/enoch/telegram/bot.py",),
        relevance="high",
        confidence="high",
        reason="PR has an inheritance label.",
        body_excerpt="Adds a recovery command.",
    )


if __name__ == "__main__":
    unittest.main()
