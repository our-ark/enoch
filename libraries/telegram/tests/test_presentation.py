from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_telegram import TELEGRAM_BREAK, render_telegram_html, telegram_message_chunks


class TelegramPresentationTests(unittest.TestCase):
    def test_renders_markdown_without_trusting_source_html(self) -> None:
        rendered = render_telegram_html(
            "\n".join(
                [
                    "## What happened",
                    "",
                    "Run `bin/enoch doctor` and keep **the result**.",
                    "Do not trust <unsafe> & text.",
                    "[PR #34](https://github.com/our-ark/enoch/pull/34)",
                ]
            )
        )

        self.assertIn("<b>What happened</b>", rendered)
        self.assertIn("<code>bin/enoch doctor</code>", rendered)
        self.assertIn("<b>the result</b>", rendered)
        self.assertIn("&lt;unsafe&gt; &amp; text.", rendered)
        self.assertIn(
            '<a href="https://github.com/our-ark/enoch/pull/34">PR #34</a>',
            rendered,
        )

    def test_distinguishes_labels_commands_and_paths(self) -> None:
        rendered = render_telegram_html(
            "\n".join(
                [
                    "Task #4 final update",
                    "Final status: failed",
                    "Check command: python -m unittest discover -s tests -t .",
                    "Path: /Users/iceberg/enoch/README.md:285",
                    "- tests: failed",
                    "Failure: validation_failed in __init__.py",
                    "/help worktree - inspect worktree help",
                ]
            )
        )

        self.assertIn("<b>Task #4 final update</b>", rendered)
        self.assertIn("<b>Final status:</b> failed", rendered)
        self.assertIn(
            "<b>Check command:</b> "
            "<code>python -m unittest discover -s tests -t .</code>",
            rendered,
        )
        self.assertIn(
            "<b>Path:</b> <code>/Users/iceberg/enoch/README.md:285</code>",
            rendered,
        )
        self.assertIn("- <b>tests:</b> failed", rendered)
        self.assertIn(
            "<b>Failure:</b> validation_failed in <code>__init__.py</code>",
            rendered,
        )
        self.assertIn(
            "<code>/help worktree</code> - inspect worktree help",
            rendered,
        )

    def test_local_markdown_links_show_copyable_paths(self) -> None:
        rendered = render_telegram_html(
            "Changed [README.md](/Users/iceberg/My Project/README.md:285)."
        )

        self.assertEqual(
            rendered,
            "Changed <code>README.md</code> "
            "(<code>/Users/iceberg/My Project/README.md:285</code>).",
        )

    def test_fenced_code_is_escaped_and_balanced(self) -> None:
        rendered = render_telegram_html(
            "Run this:\n\n```bash\npython -c '<unsafe> & data'\n```\nDone."
        )

        self.assertIn(
            "<pre><code>python -c '&lt;unsafe&gt; &amp; data'</code></pre>",
            rendered,
        )
        self.assertEqual(rendered.count("<pre><code>"), 1)
        self.assertEqual(rendered.count("</code></pre>"), 1)

    def test_long_code_blocks_split_into_valid_html_chunks(self) -> None:
        chunks = telegram_message_chunks(
            "```text\n" + ("x" * 5000) + "\n```",
            4096,
        )

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk.plain) <= 4096 for chunk in chunks))
        self.assertTrue(all(chunk.html.startswith("<pre><code>") for chunk in chunks))
        self.assertTrue(all(chunk.html.endswith("</code></pre>") for chunk in chunks))
        self.assertEqual(sum(chunk.plain.count("x") for chunk in chunks), 5000)

    def test_bare_https_urls_are_not_treated_as_paths(self) -> None:
        url = "https://zippmart.com/products/acer-ohr510?variant=50971076657404"
        rendered = render_telegram_html(f"3. Acer\n{url}")

        self.assertIn(url, rendered)
        self.assertNotIn("<code>", rendered)

    def test_explicit_break_splits_short_product_cards(self) -> None:
        text = "\n".join(
            [
                "1. JLab GO POP+ — $24.99 USD",
                "   https://www.jlab.com/products/go-pop-plus",
                TELEGRAM_BREAK,
                "2. Ulefone Buds — $29.99 USD",
                "   https://store.ulefone.com/products/buds",
                TELEGRAM_BREAK,
                "3. Acer OHR510 — $45.68 USD",
                "   https://zippmart.com/products/acer-ohr510",
            ]
        )

        chunks = telegram_message_chunks(text, 4096)

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(TELEGRAM_BREAK not in chunk.plain for chunk in chunks))
        self.assertTrue(all(TELEGRAM_BREAK not in chunk.html for chunk in chunks))
        self.assertIn("https://www.jlab.com/products/go-pop-plus", chunks[0].plain)
        self.assertNotIn("Ulefone", chunks[0].plain)
        self.assertIn("https://store.ulefone.com/products/buds", chunks[1].plain)
        self.assertIn("https://zippmart.com/products/acer-ohr510", chunks[2].plain)
        self.assertEqual(chunks[0].html.count("https://"), 1)
        self.assertEqual(chunks[1].html.count("https://"), 1)
        self.assertEqual(chunks[2].html.count("https://"), 1)

    def test_product_markdown_table_becomes_one_message_per_url(self) -> None:
        text = "\n".join(
            [
                "Enoch prepared isolated workspace task-6.",
                "",
                "I found three currently available variants under $50.",
                "",
                "| Product / merchant | Exact variant | Price | Capacity |",
                "|---|---|---:|---:|",
                "| [Jettle Travel Electric Kettle](https://jettlecompany.com/products/jettle-electric-kettle?variant=1) — Jettle Online Store | Black | $49.99 USD | 450 ml |",
                "| [Sakerplus Portable Travel Electric Tea Kettle](https://www.sakerplus.com/products/sakerplus-portable-travel-electric-tea-kettle?variant=2) — Sakerplus | 110V three-prong | $44.99 USD | 500 ml |",
                "| [Nicewell Dual-Voltage Travel Kettle](https://homejoykids1.com/products/b0dbc889sc?variant=3) — Little Sparks | Dark blue; US cord | $43.21 USD | 370 ml |",
                "",
                "My pick: the Nicewell is the lightest.",
                "No cart or checkout was created.",
            ]
        )

        chunks = telegram_message_chunks(text, 4096)

        self.assertEqual(len(chunks), 3)
        self.assertIn("https://jettlecompany.com/products/jettle-electric-kettle?variant=1", chunks[0].plain)
        self.assertNotIn("Sakerplus", chunks[0].plain)
        self.assertIn("https://www.sakerplus.com/products/sakerplus-portable-travel-electric-tea-kettle?variant=2", chunks[1].plain)
        self.assertIn("https://homejoykids1.com/products/b0dbc889sc?variant=3", chunks[2].plain)
        self.assertIn("My pick: the Nicewell is the lightest.", chunks[2].plain)
        self.assertTrue(all(chunk.plain.count("https://") == 1 for chunk in chunks))
        self.assertTrue(all("|---" not in chunk.plain for chunk in chunks))

    def test_ordinary_multi_url_prose_stays_in_one_message(self) -> None:
        text = (
            "See https://shopify.dev/docs/agents/get-started/quickstart "
            "and https://github.com/our-ark/enoch for setup."
        )

        chunks = telegram_message_chunks(text, 4096)

        self.assertEqual(len(chunks), 1)
        self.assertIn("shopify.dev", chunks[0].plain)
        self.assertIn("github.com", chunks[0].plain)

    def test_chunking_prefers_logical_text_boundaries(self) -> None:
        chunks = telegram_message_chunks("first paragraph\n\nsecond paragraph", 18)

        self.assertEqual(
            [chunk.plain for chunk in chunks],
            ["first paragraph\n\n", "second paragraph"],
        )

    def test_structured_list_entries_render_as_distinct_cards(self) -> None:
        rendered = render_telegram_html(
            "\n".join(
                [
                    "Evolve candidates:",
                    "- feedback-6638cc3d6422 [candidate feedback] Improve output",
                    "  Provenance: evidence feedback",
                    "  Score: 106",
                    "  Rationale: Records are difficult to scan.",
                    "- task-4 [candidate experience] Improve reliability",
                    "  Provenance: evidence experience",
                    "  Score: 104",
                    "  Rationale: Validation failed.",
                ]
            )
        )

        self.assertIn("<b>Evolve candidates:</b>", rendered)
        self.assertIn("<code>feedback-6638cc3d6422</code>", rendered)
        self.assertIn("<code>task-4</code>", rendered)
        self.assertEqual(rendered.count("<blockquote>"), 2)
        self.assertEqual(rendered.count("</blockquote>"), 2)
        self.assertIn(
            "<blockquote><b>Provenance:</b> evidence feedback\n"
            "<b>Score:</b> 106\n"
            "<b>Rationale:</b> Records are difficult to scan.</blockquote>",
            rendered,
        )
        self.assertIn("</blockquote>\n\n- <code>task-4</code>", rendered)

    def test_chunking_keeps_structured_entries_together(self) -> None:
        message = "\n".join(
            [
                "Items:",
                "- candidate-one First",
                "  Score: 1",
                "  Rationale: first",
                "- candidate-two Second",
                "  Score: 2",
                "  Rationale: second",
            ]
        )

        chunks = telegram_message_chunks(message, 80)

        self.assertEqual(len(chunks), 2)
        self.assertIn("candidate-one", chunks[0].plain)
        self.assertNotIn("candidate-two", chunks[0].plain)
        self.assertIn("candidate-two", chunks[1].plain)
        self.assertEqual("".join(chunk.plain for chunk in chunks), message)
        self.assertTrue(all(chunk.html.count("<blockquote>") == 1 for chunk in chunks))

    def test_ordinary_bullets_do_not_render_as_cards(self) -> None:
        rendered = render_telegram_html(
            "\n".join(
                [
                    "Ideas:",
                    "- make the output easier to scan",
                    "  without changing its content",
                    "  or assuming a particular schema",
                    "- preserve ordinary prose",
                ]
            )
        )

        self.assertNotIn("<blockquote>", rendered)
        self.assertIn("- make the output easier to scan", rendered)

    def test_worktree_cards_distinguish_branches_and_paths(self) -> None:
        rendered = render_telegram_html(
            "\n".join(
                [
                    "Task worktrees (1):",
                    "- task path #9 [clean] enoch/main-task-9-change",
                    "  Tasks: #9 [completed]",
                    "  Path: /Users/iceberg/.enoch-task-worktrees/enoch/task-9",
                ]
            )
        )

        self.assertIn("<code>enoch/main-task-9-change</code>", rendered)
        self.assertIn(
            "<b>Path:</b> "
            "<code>/Users/iceberg/.enoch-task-worktrees/enoch/task-9</code>",
            rendered,
        )
        self.assertEqual(rendered.count("<blockquote>"), 1)


if __name__ == "__main__":
    unittest.main()
