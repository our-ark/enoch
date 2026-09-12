from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.lineage.core import (
    APPLICABILITY_APPLICABLE,
    APPLICABILITY_NOT_APPLICABLE,
    ASSESSMENT_ASSESSED,
    ASSESSMENT_FAILED,
    LineageCandidate,
    LineageError,
    ParentLink,
    STATUS_ADOPTED,
    find_inbox_candidate,
    format_candidate,
    format_inbox,
    format_lineage,
    format_parent_inherit_report,
    format_refresh_report,
    lineage_adaptation_request,
    lineage_inbox_file,
    load_birth_commit,
    load_current_agent_profile,
    load_inbox_candidates,
    load_lineage_inbox_report,
    load_parent,
    mark_inbox_candidate,
    parse_declared_skills,
    parse_identity_name,
    parse_lineage_birth_commit,
    parse_lineage_parent,
    refresh_lineage_inbox,
    resolve_lineage,
)
from enoch.lineage.assessment import assess_lineage_inbox
from enoch.commands import inherit_command
from enoch.lineage.lifecycle import (
    lineage_context_source,
    reconcile_lineage_adoptions,
)
from enoch.lineage.config import lineage_settings
from enoch.providers.contracts import (
    RepositoryRevision,
    ReviewIdentity,
    ReviewRecord,
    ReviewVersion,
)
from enoch.tasks.queue import TaskJob


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

    def test_parse_lineage_birth_provenance(self) -> None:
        parent_commit = "a" * 40
        birth_commit = "b" * 40
        text = "\n".join(
            [
                "schema_version: 1",
                "parent:",
                "  name: Seth",
                "  repo: https://github.com/our-ark/seth",
                "  branch: main",
                f"  commit_at_birth: {parent_commit}",
                "descendant:",
                f"  birth_commit: {birth_commit}",
            ]
        )

        self.assertEqual(
            parse_lineage_parent(text),
            ParentLink(
                name="Seth",
                repo="our-ark/seth",
                branch="main",
                commit_at_birth=parent_commit,
            ),
        )
        self.assertEqual(parse_lineage_birth_commit(text), birth_commit)

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

    def test_load_birth_commit_from_agent_lineage_file(self) -> None:
        birth_commit = "b" * 40
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".agent" / "lineage.yaml"
            path.parent.mkdir()
            path.write_text(
                "\n".join(
                    [
                        "parent:",
                        "  name: Seth",
                        "  repo: our-ark/seth",
                        "descendant:",
                        f"  birth_commit: {birth_commit}",
                    ]
                ),
                encoding="utf-8",
            )

            loaded = load_birth_commit(root)

        self.assertEqual(loaded, birth_commit)

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

    def test_resolve_lineage_reads_parent_metadata_at_immutable_birth_commit(self) -> None:
        parent_commit = "a" * 40
        client = RecordingLineageClient()
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp), commit_at_birth=parent_commit)

            resolution = resolve_lineage(root, client=client)
            formatted = format_lineage(resolution.ancestors)

        self.assertEqual(client.remote_parent_calls, [("our-ark/enoch", parent_commit)])
        self.assertEqual(resolution.ancestors[0].commit_at_birth, parent_commit)
        self.assertIn(f"Parent at birth: {parent_commit[:12]}", formatted)

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
        self.assertIn("   Source: src/enoch/body.yaml", formatted)
        self.assertIn(
            "   New skills: skill-library, evolve",
            formatted,
        )

    def test_load_current_agent_profile_from_identity_yaml(self) -> None:
        current_agent = load_current_agent_profile(ROOT)

        self.assertIsNotNone(current_agent)
        assert current_agent is not None
        self.assertEqual(current_agent.name, "Enoch")
        self.assertIn("code", current_agent.skills)
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
        self.assertEqual(candidate.relevance, "unassessed")
        self.assertEqual(candidate.assessment_status, "pending")
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
        self.assertIn("direct change", candidate.diff_excerpt)
        self.assertIn("Committed at: 2026-06-21T16:37:27Z", format_candidate(candidate))
        self.assertIn("Commit: enoch-direct-sha", format_candidate(candidate))

    def test_pr_is_assessed_once_instead_of_as_each_constituent_commit(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(
                root,
                scope="parent",
                client=MultiCommitPrLineageClient(),
            )

        self.assertEqual(report.new_count, 1)
        self.assertEqual(
            [candidate.id for candidate in report.candidates],
            ["our-ark/enoch#44"],
        )

    def test_missing_diff_is_retried_after_the_discovery_cursor_advances(self) -> None:
        client = FlakyDiffLineageClient()
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            refresh_lineage_inbox(root, scope="parent", client=client)
            first = find_inbox_candidate("our-ark/enoch#32", root)
            refresh_lineage_inbox(root, scope="parent", client=client)
            retried = find_inbox_candidate("our-ark/enoch#32", root)

        assert first is not None
        assert retried is not None
        self.assertEqual(first.diff_excerpt, "")
        self.assertIn("PR 32 change", retried.diff_excerpt)
        self.assertEqual(client.diff_calls, 2)

    def test_refresh_parent_only_stores_direct_parent_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            inbox_ids = [item.id for item in load_inbox_candidates(root)]

        self.assertEqual(report.scope, "parent")
        self.assertEqual(inbox_ids, ["our-ark/enoch#32"])
        self.assertIn("direct parent", format_refresh_report(report))

    def test_incremental_cursor_discovers_more_than_twenty_new_commits(self) -> None:
        client = IncrementalLineageClient()
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            first = refresh_lineage_inbox(root, scope="parent", client=client)
            client.add_new_commits(25)

            second = refresh_lineage_inbox(root, scope="parent", client=client)
            ids = {
                candidate.id
                for candidate in load_inbox_candidates(root)
            }

        self.assertEqual(first.new_count, 1)
        self.assertEqual(second.new_count, 25)
        self.assertEqual(len(ids), 26)
        self.assertFalse(second.errors)

    def test_cursor_is_not_advanced_when_scan_limit_cannot_reach_it(self) -> None:
        client = IncrementalLineageClient()
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "lineage:\n  scan_limit: 20\n",
                encoding="utf-8",
            )
            refresh_lineage_inbox(root, scope="parent", client=client)
            client.add_new_commits(25)

            failed = refresh_lineage_inbox(root, scope="parent", client=client)
            payload = json.loads(lineage_inbox_file(root).read_text(encoding="utf-8"))

        self.assertTrue(failed.errors)
        self.assertIn("No cursor was advanced", failed.errors[-1])
        self.assertEqual(payload["latest_heads"]["our-ark/enoch"], "base-sha")

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

    def test_parent_inherit_report_keeps_unassessed_changes_visible(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))

            report = refresh_lineage_inbox(root, scope="parent", client=LowRelevanceLineageClient())
            stored = load_inbox_candidates(root)
            formatted = format_parent_inherit_report(report)

        self.assertEqual([candidate.id for candidate in stored], ["our-ark/enoch#99"])
        self.assertEqual(stored[0].relevance, "unassessed")
        self.assertIn("Awaiting assessment:", formatted)
        self.assertIn("our-ark/enoch#99 Update README wording", formatted)

    def test_lineage_settings_use_semantic_assessment_defaults(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "lineage:",
                        "  assessment_batch_size: 4",
                        "  max_diff_chars: 9000",
                        "  scan_limit: 250",
                    ]
                ),
                encoding="utf-8",
            )

            settings = lineage_settings(root)

        self.assertEqual(settings.assessment_batch_size, 4)
        self.assertEqual(settings.max_diff_chars, 9000)
        self.assertEqual(settings.scan_limit, 250)

    def test_lineage_assessment_limits_are_configurable_and_bounded(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(
                    [
                        "lineage:",
                        "  assessment_batch_size: 500",
                        "  max_diff_chars: 20",
                        "  scan_limit: 10000",
                    ]
                ),
                encoding="utf-8",
            )
            settings = lineage_settings(root)

        self.assertEqual(settings.assessment_batch_size, 20)
        self.assertEqual(settings.max_diff_chars, 1_000)
        self.assertEqual(settings.scan_limit, 5_000)

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

    def test_format_candidate_and_adaptation_request_include_context(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())
            candidate = find_inbox_candidate("our-ark/enoch#32", root)

        assert candidate is not None
        self.assertIn("Status: pending", format_candidate(candidate))
        self.assertIn("Adapt direct-parent change", lineage_adaptation_request(candidate))
        self.assertIn("normal pull-request workflow", lineage_adaptation_request(candidate))

    def test_codex_assesses_every_new_change_and_caches_by_source(self) -> None:
        calls: list[str] = []
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            report = refresh_lineage_inbox(
                root,
                scope="parent",
                client=FakeLineageClient(),
            )

            def generator(prompt: str) -> str:
                calls.append(prompt)
                self.assertIn("untrusted repository data", prompt)
                return json.dumps(
                    [
                        {
                            "change_id": "our-ark/enoch#32",
                            "summary": "Adds a configurable reasoning command.",
                            "behavioral_change": "Users can select reasoning effort.",
                            "applicability": "applicable",
                            "rationale": "Enoch exposes runtime configuration.",
                            "proposed_adaptation": "Adapt the provider-neutral setting.",
                            "risks": ["Model capabilities differ."],
                            "likely_files": ["src/enoch/commands.py"],
                            "suggested_tests": ["Verify supported effort values."],
                            "confidence": "high",
                        }
                    ]
                )

            assessed = assess_lineage_inbox(
                report,
                root,
                generator=generator,
                mission="Help Roy operate and improve Enoch.",
            )
            candidate = find_inbox_candidate("our-ark/enoch#32", root)
            cached = assess_lineage_inbox(
                assessed,
                root,
                generator=lambda _prompt: self.fail("cached change was reassessed"),
                mission="Help Roy operate and improve Enoch.",
            )

        assert candidate is not None
        self.assertEqual(len(calls), 1)
        self.assertEqual(assessed.assessed_count, 1)
        self.assertEqual(cached.assessed_count, 0)
        self.assertEqual(candidate.assessment_status, ASSESSMENT_ASSESSED)
        self.assertEqual(candidate.applicability, APPLICABILITY_APPLICABLE)
        self.assertEqual(candidate.summary, "Adds a configurable reasoning command.")
        self.assertIn("Applicable:", format_parent_inherit_report(assessed))
        self.assertIn("Codex assessment:", format_candidate(candidate))

    def test_assessment_does_not_process_an_old_inbox_without_a_parent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / ".agent" / "lineage_inbox.json"
            legacy.parent.mkdir()
            legacy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "candidates": [_lineage_candidate_fixture().__dict__],
                    }
                ),
                encoding="utf-8",
            )
            report = refresh_lineage_inbox(
                root,
                scope="parent",
                client=FakeLineageClient(),
            )

            assessed = assess_lineage_inbox(
                report,
                root,
                generator=lambda _prompt: self.fail(
                    "an unconfigured parent must not trigger assessment"
                ),
                mission="Help Roy operate Enoch.",
            )

        self.assertEqual(assessed.ancestors, ())
        self.assertEqual(assessed.candidates, ())
        self.assertEqual(assessed.assessed_count, 0)

    def test_failed_assessment_preserves_change_for_retry(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            report = refresh_lineage_inbox(
                root,
                scope="parent",
                client=FakeLineageClient(),
            )

            failed = assess_lineage_inbox(
                report,
                root,
                generator=lambda _prompt: "not json",
                mission="Help Roy operate Enoch.",
            )
            candidate = find_inbox_candidate("our-ark/enoch#32", root)

        assert candidate is not None
        self.assertEqual(failed.assessment_failed_count, 1)
        self.assertEqual(candidate.assessment_status, ASSESSMENT_FAILED)
        self.assertEqual(candidate.status, "pending")
        self.assertIn("malformed JSON", candidate.assessment_error)

    def test_assessment_uses_configurable_fresh_batches(self) -> None:
        calls = 0
        progress = []
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "lineage:\n  assessment_batch_size: 2\n",
                encoding="utf-8",
            )
            report = refresh_lineage_inbox(
                root,
                scope="all",
                client=FakeLineageClient(),
            )

            def generator(prompt: str) -> str:
                nonlocal calls
                calls += 1
                raw_records = next(
                    line.removeprefix("Untrusted lineage changes: ")
                    for line in prompt.splitlines()
                    if line.startswith("Untrusted lineage changes: ")
                )
                records = json.loads(raw_records)
                return json.dumps(
                    [
                        _assessment_payload(record["change_id"])
                        for record in records
                    ]
                )

            assessed = assess_lineage_inbox(
                report,
                root,
                generator=generator,
                mission="Help Roy operate Enoch.",
                progress_callback=progress.append,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(assessed.assessed_count, 3)
        self.assertEqual(
            [update.processed_count for update in progress],
            [2, 3],
        )
        self.assertEqual(
            [update.batch_index for update in progress],
            [1, 2],
        )

    def test_stored_inherit_inbox_does_not_refresh(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refresh_lineage_inbox(root, scope="parent", client=FakeLineageClient())

            reply = inherit_command(
                "/inherit inbox",
                root,
                refresh_lineage_fn=lambda *_args, **_kwargs: self.fail(
                    "/inherit inbox must not scan the forge"
                ),
            )

        self.assertIn("Direct parent inheritance inbox:", reply)
        self.assertIn("our-ark/enoch#32", reply)
        self.assertIn("/inherit inspect <change_id>", reply)

    def test_stored_lineage_report_reconstructs_scan_context(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            refreshed = refresh_lineage_inbox(
                root,
                scope="parent",
                client=FakeLineageClient(),
            )

            loaded = load_lineage_inbox_report(root, scope="parent")

        self.assertEqual(loaded.scope, "parent")
        self.assertEqual(loaded.ancestors, refreshed.ancestors)
        self.assertEqual(
            tuple(candidate.id for candidate in loaded.candidates),
            tuple(candidate.id for candidate in refreshed.candidates),
        )
        self.assertEqual(loaded.latest_heads, refreshed.latest_heads)

    def test_legacy_inbox_is_read_then_migrated_to_private_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            legacy = root / ".agent" / "lineage_inbox.json"
            candidate = _lineage_candidate_fixture()
            legacy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "candidates": [candidate.__dict__],
                    }
                ),
                encoding="utf-8",
            )

            loaded = find_inbox_candidate(candidate.id, root)
            mark_inbox_candidate(candidate.id, "ignored", root)
            migrated = json.loads(lineage_inbox_file(root).read_text(encoding="utf-8"))

            self.assertIsNotNone(loaded)
            self.assertTrue(lineage_inbox_file(root).exists())
            self.assertTrue(legacy.exists())
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["latest_heads"], {})

    def test_landed_review_is_required_before_linked_change_becomes_adopted(self) -> None:
        with TemporaryDirectory() as temp:
            root = _root_with_parent(Path(temp))
            report = refresh_lineage_inbox(
                root,
                scope="parent",
                client=FakeLineageClient(),
            )
            assess_lineage_inbox(
                report,
                root,
                generator=lambda _prompt: _assessment_json(
                    "our-ark/enoch#32",
                    applicability=APPLICABILITY_NOT_APPLICABLE,
                ),
                mission="Help Roy operate Enoch.",
            )
            from enoch.lineage.core import link_inbox_candidate

            link_inbox_candidate("our-ark/enoch#32", 7, root)
            task = TaskJob(
                id=7,
                chat_id=42,
                text="adapt",
                created_at="2026-07-26T00:00:00+00:00",
                status="completed",
                context_source=lineage_context_source("our-ark/enoch#32"),
                review_url="https://github.com/our-ark/enoch/pull/77",
                review_urls=("https://github.com/our-ark/enoch/pull/77",),
            )

            open_result = reconcile_lineage_adoptions(
                root,
                (task,),
                review=FakeReviewProvider(state="open"),
            )
            linked = find_inbox_candidate("our-ark/enoch#32", root)
            landed_result = reconcile_lineage_adoptions(
                root,
                (task,),
                review=FakeReviewProvider(state="landed"),
            )
            adopted = find_inbox_candidate("our-ark/enoch#32", root)

        assert linked is not None
        assert adopted is not None
        self.assertEqual(open_result.adopted_ids, ())
        self.assertEqual(linked.status, "linked")
        self.assertEqual(landed_result.adopted_ids, ("our-ark/enoch#32",))
        self.assertEqual(adopted.status, STATUS_ADOPTED)
        self.assertEqual(adopted.adopted_revision, "landed-sha")


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
        return {"our-ark/enoch": "enoch-merge", "our-ark/lucy": "lucy-docs"}[repo]

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
            return ("src/enoch/app/core.py",)
        if number == 7:
            return ("src/enoch/immune.py",)
        return ("README.md",)

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        shas = (
            ("enoch-merge",)
            if repo == "our-ark/enoch"
            else ("lucy-docs", "lucy-merge")
        )
        return [
            {
                "sha": sha,
                "commit": {
                    "message": f"Merge {sha}",
                    "committer": {"date": "2026-06-17T03:00:00Z"},
                },
            }
            for sha in shas[:limit]
        ]

    def commit_files(self, repo: str, sha: str) -> tuple[str, ...]:
        return ()

    def pr_diff(self, repo: str, number: int) -> str:
        return f"diff --git a/source b/source\n+PR {number} change"

    def commit_diff(self, repo: str, sha: str) -> str:
        return f"diff --git a/source b/source\n+direct change {sha}"


class DirectCommitLineageClient(FakeLineageClient):
    def latest_commit(self, repo: str, branch: str) -> str:
        return "enoch-direct-sha"

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
    def latest_commit(self, repo: str, branch: str) -> str:
        return "enoch-docs"

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

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return [
            {
                "sha": "enoch-docs",
                "commit": {
                    "message": "Update README wording",
                    "committer": {"date": "2026-06-17T03:00:00Z"},
                },
            }
        ]


class MultiCommitPrLineageClient(FakeLineageClient):
    def latest_commit(self, repo: str, branch: str) -> str:
        return "merge-commit"

    def merged_prs(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return [
            {
                "number": 44,
                "title": "Add one behavior through two commits",
                "body": "One logical pull request.",
                "labels": [],
                "mergedAt": "2026-07-26T00:00:00Z",
                "mergeCommit": {"oid": "merge-commit"},
                "url": "https://github.com/our-ark/enoch/pull/44",
            }
        ]

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return [
            _commit_payload("merge-commit"),
            _commit_payload("pr-commit-2"),
            _commit_payload("pr-commit-1"),
        ]

    def pr_commits(self, repo: str, number: int) -> tuple[str, ...]:
        return ("pr-commit-1", "pr-commit-2")


class FlakyDiffLineageClient(FakeLineageClient):
    def __init__(self) -> None:
        self.diff_calls = 0

    def pr_diff(self, repo: str, number: int) -> str:
        self.diff_calls += 1
        if self.diff_calls == 1:
            raise OSError("temporary diff failure")
        return super().pr_diff(repo, number)


class IncrementalLineageClient(FakeLineageClient):
    def __init__(self) -> None:
        self.items = [_commit_payload("base-sha")]

    def add_new_commits(self, count: int) -> None:
        self.items = [
            _commit_payload(f"new-{index:03d}-sha")
            for index in range(count, 0, -1)
        ] + self.items

    def latest_commit(self, repo: str, branch: str) -> str:
        return str(self.items[0]["sha"])

    def merged_prs(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return []

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict]:
        return self.items[:limit]

    def commit_files(self, repo: str, sha: str) -> tuple[str, ...]:
        return (f"src/enoch/{sha}.py",)


class RecordingLineageClient(FakeLineageClient):
    def __init__(self) -> None:
        self.remote_parent_calls: list[tuple[str, str]] = []

    def remote_parent(self, repo: str, branch: str) -> ParentLink | None:
        self.remote_parent_calls.append((repo, branch))
        return super().remote_parent(repo, branch)


class BlockedLineageClient(FakeLineageClient):
    def remote_parent(self, repo: str, branch: str) -> ParentLink | None:
        raise LineageError("private repo or missing permissions")


def _root_with_parent(root: Path, *, commit_at_birth: str = "") -> Path:
    path = root / ".agent" / "lineage.yaml"
    path.parent.mkdir()
    lines = ["parent:", "  name: Enoch", "  repo: our-ark/enoch"]
    if commit_at_birth:
        lines.append(f"  commit_at_birth: {commit_at_birth}")
    path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    return root


def _assessment_json(
    change_id: str,
    *,
    applicability: str = "applicable",
) -> str:
    return json.dumps([_assessment_payload(change_id, applicability=applicability)])


def _assessment_payload(
    change_id: str,
    *,
    applicability: str = "applicable",
) -> dict:
    return {
        "change_id": change_id,
        "summary": "Summary.",
        "behavioral_change": "Behavior changes.",
        "applicability": applicability,
        "rationale": "Rationale.",
        "proposed_adaptation": "Adapt it.",
        "risks": [],
        "likely_files": [],
        "suggested_tests": [],
        "confidence": "medium",
    }


def _commit_payload(sha: str) -> dict:
    return {
        "sha": sha,
        "html_url": f"https://github.com/our-ark/enoch/commit/{sha}",
        "commit": {
            "message": f"Change {sha}",
            "committer": {"date": "2026-07-26T00:00:00Z"},
        },
    }


def _lineage_candidate_fixture() -> LineageCandidate:
    return LineageCandidate(
        id="our-ark/enoch#32",
        repo="our-ark/enoch",
        pr_number=32,
        title="Add Telegram thinking level command",
        url="https://github.com/our-ark/enoch/pull/32",
        merged_at="2026-06-17T01:31:12Z",
        merge_commit="enoch-merge",
        ancestor_name="Enoch",
        depth=1,
        labels=(),
        files=("src/enoch/app/core.py",),
        relevance="unassessed",
        confidence="unknown",
        reason="Awaiting Codex assessment.",
        body_excerpt="Adds /thinking.",
    )


class FakeReviewProvider:
    def __init__(self, *, state: str) -> None:
        self.state = state

    def inspect_review(self, identity: ReviewIdentity, root: Path) -> ReviewRecord:
        del root
        revision = RepositoryRevision("landed-sha")
        return ReviewRecord(
            identity=identity,
            title="Adapt ancestor change",
            body="",
            state=self.state,
            versions=(ReviewVersion("v1", revision),),
            landed_revision=revision if self.state == "landed" else None,
            landed_at="2026-07-26T01:00:00Z" if self.state == "landed" else "",
        )


if __name__ == "__main__":
    unittest.main()
