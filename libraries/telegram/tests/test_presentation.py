from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_telegram import render_telegram_html, telegram_message_chunks


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

    def test_chunking_prefers_logical_text_boundaries(self) -> None:
        chunks = telegram_message_chunks("first paragraph\n\nsecond paragraph", 18)

        self.assertEqual(
            [chunk.plain for chunk in chunks],
            ["first paragraph\n\n", "second paragraph"],
        )


if __name__ == "__main__":
    unittest.main()
