from __future__ import annotations

from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.lineage.core import (
    LineageError,
    ParentLink,
    find_inbox_candidate,
    format_candidate,
    format_inbox,
    format_lineage,
    format_parent_inherit_report,
    format_refresh_report,
    lineage_adopt_prompt,
    lineage_inbox_file,
    load_current_agent_profile,
    load_inbox_candidates,
    load_parent,
    mark_inbox_candidate,
    parse_declared_skills,
    parse_identity_name,
    parse_lineage_parent,
    refresh_lineage_inbox,
    resolve_lineage,
)
from enoch.lineage.config import (
    DEFAULT_IMPORTANT_FILE_PREFIXES,
    DEFAULT_IMPORTANT_TITLE_WORDS,
    lineage_settings,
)


class EnochLineageTests(unittest.TestCase):
    def test_parse_lineage_parent(self) -> None:
        parent = parse_lineage_parent(
            "\n".join(
                [
                    "parent:",
                    "  name: Enoch",
                    "  repo: our-ark/enoch",
                    "  branch: main",
                ]
            )
        )

        self.assertEqual(parent, ParentLink(name="Enoch", repo="our-ark/enoch", branch="main"))

    def test_parse_lineage_parent_normalizes_github_url(self) -> None:
        parent = parse_lineage_parent(
            "\n".join(
                [
                    "parent:",
                    "  name: Lucy",
                    "  repo: https://github.com/our-ark/lucy",
                    "  branch: main",
                ]
            )
        )

        self.assertEqual(parent, ParentLink(name="Lucy", repo="our-ark/lucy", branch="main"))

    def test_load_parent_from_agent_lineage_file(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".agent" / "lineage.yaml"
            path.parent.mkdir()
            path.write_text(
                "\n".join(["parent:", "  name: Enoch", "  repo: our-ark/enoch"]),
                encoding="utf-8",
            )

            parent = load_parent(root)

        self.assertEqual(parent, ParentLink(name="Enoch", repo="our-ark/enoch", branch="main"))

    def test_resolves_ancestor_chain_recursively(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            chain = resolve_lineage(root, client=FakeLineageClient()).ancestors

        self.assertEqual([item.name for item in chain], ["Enoch", "Lucy"])
        self.assertEqual([item.depth for item in chain], [1, 2])
        self.assertIn("Lucy", format_lineage(chain))

    def test_resolve_lineage_stops_at_lucy_root_ancestor(self) -> None:
        client = RecordingLineageClient()
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            resolution = resolve_lineage(root, client=client)

        self.assertEqual([item.name for item in resolution.ancestors], ["Enoch", "Lucy"])
        self.assertEqual(client.remote_parent_calls, [("our-ark/enoch", "main")])
        self.assertEqual(resolution.warnings, ())

    def test_format_lineage_includes_pending_change_counts(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, client=FakeLineageClient())

        current_agent = load_current_agent_profile(ROOT)
        formatted = format_lineage(report.ancestors, candidates=report.candidates, current_agent=current_agent)
        self.assertIn("Ancestor chain", formatted)
        self.assertIn("1. Lucy", formatted)
        self.assertIn("   Relation: root ancestor", formatted)
        self.assertIn("   Repo: our-ark/lucy@main", formatted)
        self.assertIn("   New skills: itu-talk, code, teach, learn", formatted)
        self.assertIn("   Pending: 2 changes", formatted)
        self.assertIn("2. Enoch", formatted)
        self.assertIn("   Relation: parent", formatted)
        self.assertIn("   Repo: our-ark/enoch@main", formatted)
        self.assertIn("   New skills: telegram-talk, inherit, work", formatted)
        self.assertNotIn("teach (hidden)", formatted)
        self.assertIn("   Pending: 1 change", formatted)
        self.assertIn("3. Enoch (current)", formatted)
        self.assertIn("   Relation: current agent", formatted)
        self.assertIn("   Source: src/enoch/identity.yaml", formatted)
        self.assertIn("   New skills: github, evolve", formatted)

    def test_load_current_agent_profile_from_identity_yaml(self) -> None:
        current_agent = load_current_agent_profile(ROOT)

        self.assertIsNotNone(current_agent)
        assert current_agent is not None
        self.assertEqual(current_agent.name, "Enoch")
        self.assertIn("github", current_agent.skills)
        self.assertIn("evolve", current_agent.skills)

    def test_parse_declared_skills_from_identity_yaml(self) -> None:
        self.assertEqual(
            parse_declared_skills(
                "\n".join(
                    [
                        "name: Enoch",
                        "skills:",
                        "  - name: code",
                        "    path: src/enoch/skills/code",
                        "  - name: work",
                        "  - name: teach",
                        "    exposure: hidden",
                    ]
                )
            ),
            ("code", "work"),
        )

    def test_parse_identity_name_from_identity_yaml(self) -> None:
        self.assertEqual(parse_identity_name("name: Enoch\nkind: agent\n"), "Enoch")

    def test_resolve_lineage_reports_inaccessible_parent_lineage(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            resolution = resolve_lineage(root, client=BlockedLineageClient())
            formatted = format_lineage(resolution.ancestors, resolution.warnings)

        self.assertEqual([item.name for item in resolution.ancestors], ["Enoch"])
        self.assertEqual(len(resolution.warnings), 1)
        self.assertIn("Could not read parent lineage from our-ark/enoch@main", resolution.warnings[0])
        self.assertIn("Warnings:", formatted)
        self.assertIn("private repo", formatted)

    def test_refresh_report_includes_lineage_resolution_warnings(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, client=BlockedLineageClient())

        self.assertTrue(report.errors)
        self.assertIn("private repo", format_refresh_report(report))

    def test_refresh_all_ancestors_stores_inbox_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, client=FakeLineageClient())
            candidate = find_inbox_candidate("our-ark/enoch#32", root)
            inbox_ids = {item.id for item in load_inbox_candidates(root)}
            inbox_exists = lineage_inbox_file(root).exists()
            inbox_text = format_inbox(load_inbox_candidates(root))

        self.assertEqual(report.scope, "all")
        self.assertEqual(report.new_count, 3)
        self.assertEqual(inbox_ids, {"our-ark/enoch#32", "our-ark/lucy#7", "our-ark/lucy#8"})
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.relevance, "medium")
        self.assertTrue(inbox_exists)
        self.assertIn("Ancestor refresh checked all ancestors", format_refresh_report(report))
        self.assertIn("our-ark/lucy#7", inbox_text)

    def test_refresh_stores_direct_commit_changes(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, scope="parent", client=DirectCommitLineageClient())
            candidate = find_inbox_candidate("enoch-direct", root)

        self.assertEqual(report.new_count, 1)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.pr_number, 0)
        self.assertEqual(candidate.title, "Add direct ancestor commit")
        self.assertEqual(candidate.merge_commit, "enoch-direct-sha")
        self.assertIn("Committed at: 2026-06-21T16:37:27Z", format_candidate(candidate))
        self.assertIn("Commit: enoch-direct-sha", format_candidate(candidate))

    def test_refresh_parent_only_stores_direct_parent_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            inbox_ids = [item.id for item in load_inbox_candidates(root)]

        self.assertEqual(report.scope, "parent")
        self.assertEqual(inbox_ids, ["our-ark/enoch#32"])
        self.assertIn("direct parent", format_refresh_report(report))

    def test_parent_inherit_report_excludes_grandparent_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refresh_lineage_inbox(root, scope="all", client=FakeLineageClient())

            report = refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            formatted = format_parent_inherit_report(report)

        self.assertIn("Direct parent inheritance checked.", formatted)
        self.assertIn("our-ark/enoch#32", formatted)
        self.assertNotIn("our-ark/lucy#7", formatted)
        self.assertNotIn("our-ark/lucy#8", formatted)

    def test_parent_inherit_report_only_lists_inheritable_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, scope="parent", client=LowRelevanceLineageClient())
            stored = load_inbox_candidates(root)
            formatted = format_parent_inherit_report(report)

        self.assertEqual([candidate.id for candidate in stored], ["our-ark/enoch#99"])
        self.assertEqual(stored[0].relevance, "low")
        self.assertIn("No pending direct-parent inheritance candidates.", formatted)
        self.assertIn("Filtered out 1 parent change(s)", formatted)
        self.assertNotIn("our-ark/enoch#99 Update README wording", formatted)

    def test_lineage_settings_use_defaults_and_config_overrides(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "lineage:",
                        "  important_title_words: thinking, upgrade",
                        "  important_file_prefixes: README.md, docs/",
                    ]
                ),
                encoding="utf-8",
            )

            settings = lineage_settings(root)

        self.assertEqual(settings.important_title_words, ("thinking", "upgrade"))
        self.assertEqual(settings.important_file_prefixes, ("README.md", "docs/"))
        self.assertIn("fix", DEFAULT_IMPORTANT_TITLE_WORDS)
        self.assertIn("src/enoch/telegram/bot.py", DEFAULT_IMPORTANT_FILE_PREFIXES)
        self.assertNotIn("src/enoch/agency.py", DEFAULT_IMPORTANT_FILE_PREFIXES)

    def test_lineage_title_ranking_uses_configured_words(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "lineage:",
                        "  important_title_words: thinking",
                    ]
                ),
                encoding="utf-8",
            )

            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            candidate = find_inbox_candidate("our-ark/enoch#32", root)

        assert candidate is not None
        self.assertEqual(candidate.relevance, "high")
        self.assertIn("Title suggests", candidate.reason)

    def test_ignore_hides_candidate_from_pending_inbox(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())

            ignored = mark_inbox_candidate("our-ark/enoch#32", "ignored", root, note="not needed")
            pending = load_inbox_candidates(root)
            inactive = load_inbox_candidates(root, include_inactive=True)

        self.assertEqual(ignored.status, "ignored")
        self.assertEqual(pending, ())
        self.assertEqual(inactive[0].status, "ignored")
        self.assertIn("No pending", format_inbox(pending))

    def test_refresh_preserves_reviewed_candidate_status(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            mark_inbox_candidate("our-ark/enoch#32", "ignored", root, note="not needed")

            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            candidate = find_inbox_candidate("our-ark/enoch#32", root)
            pending_after_refresh = load_inbox_candidates(root)

        assert candidate is not None
        self.assertEqual(candidate.status, "ignored")
        self.assertEqual(pending_after_refresh, ())

    def test_format_candidate_and_adopt_prompt_include_context(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            candidate = find_inbox_candidate("our-ark/enoch#32", root)

        assert candidate is not None
        self.assertIn("Status: pending", format_candidate(candidate))
        self.assertIn("Consider whether Enoch should adapt", lineage_adopt_prompt(candidate))
        self.assertIn("src/enoch/telegram/bot.py", lineage_adopt_prompt(candidate))


class FakeLineageClient:
    def remote_parent(self, repo: str, branch: str) -> ParentLink | None:
        if repo == "our-ark/enoch":
            return ParentLink(name="Lucy", repo="our-ark/lucy", branch="main")
        return None

    def declared_skills(self, repo: str, branch: str) -> tuple[str, ...]:
        return {
            "our-ark/enoch": ("telegram-talk", "code", "inherit", "work", "learn"),
            "our-ark/lucy": ("itu-talk", "code", "teach", "learn"),
        }.get(repo, ())

    def latest_commit(self, repo: str, branch: str) -> str:
        return {"our-ark/enoch": "enoch-head", "our-ark/lucy": "lucy-head"}[repo]

    def merged_prs(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        if repo == "our-ark/enoch":
            return [
                {
                    "number": 32,
                    "title": "Add Telegram thinking level command",
                    "body": "Adds /thinking.",
                    "labels": [],
                    "mergedAt": "2026-06-17T01:31:12Z",
                    "mergeCommit": {"oid": "enoch-merge"},
                    "url": "https://github.com/our-ark/enoch/pull/32",
                }
            ]
        return [
            {
                "number": 7,
                "title": "Fix doctor rollback",
                "body": "Important runtime fix.",
                "labels": [],
                "mergedAt": "2026-06-17T02:00:00Z",
                "mergeCommit": {"oid": "lucy-merge"},
                "url": "https://github.com/our-ark/lucy/pull/7",
            },
            {
                "number": 8,
                "title": "Update README wording",
                "body": "Cosmetic docs.",
                "labels": [],
                "mergedAt": "2026-06-17T03:00:00Z",
                "mergeCommit": {"oid": "lucy-docs"},
                "url": "https://github.com/our-ark/lucy/pull/8",
            },
        ]

    def pr_files(self, repo: str, number: int) -> tuple[str, ...]:
        if repo == "our-ark/enoch":
            return ("src/enoch/telegram/bot.py",)
        if number == 7:
            return ("src/enoch/immune.py",)
        return ("README.md",)

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return []

    def commit_files(self, repo: str, sha: str) -> tuple[str, ...]:
        return ()


class DirectCommitLineageClient(FakeLineageClient):
    def merged_prs(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return []

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return [
            {
                "sha": "enoch-direct-sha",
                "html_url": "https://github.com/our-ark/enoch/commit/enoch-direct-sha",
                "commit": {
                    "message": "Add direct ancestor commit\n\nDetailed notes.",
                    "committer": {"date": "2026-06-21T16:37:27Z"},
                },
            }
        ]

    def commit_files(self, repo: str, sha: str) -> tuple[str, ...]:
        return ("src/enoch/skills/learn/SKILL.md",)


class LowRelevanceLineageClient(FakeLineageClient):
    def merged_prs(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        if repo != "our-ark/enoch":
            return []
        return [
            {
                "number": 99,
                "title": "Update README wording",
                "body": "Cosmetic docs.",
                "labels": [],
                "mergedAt": "2026-06-17T03:00:00Z",
                "mergeCommit": {"oid": "enoch-docs"},
                "url": "https://github.com/our-ark/enoch/pull/99",
            }
        ]

    def pr_files(self, repo: str, number: int) -> tuple[str, ...]:
        return ("README.md",)


class RecordingLineageClient(FakeLineageClient):
    def __init__(self) -> None:
        self.remote_parent_calls: list[tuple[str, str]] = []

    def remote_parent(self, repo: str, branch: str) -> ParentLink | None:
        self.remote_parent_calls.append((repo, branch))
        return super().remote_parent(repo, branch)


class BlockedLineageClient(FakeLineageClient):
    def remote_parent(self, repo: str, branch: str) -> ParentLink | None:
        raise LineageError("private repo or missing permissions")


def _root_with_parent(root: Path) -> Path:
    path = root / ".agent" / "lineage.yaml"
    path.parent.mkdir()
    path.write_text(
        "\n".join(["parent:", "  name: Enoch", "  repo: our-ark/enoch"]),
        encoding="utf-8",
    )
    return root


if __name__ == "__main__":
    unittest.main()
