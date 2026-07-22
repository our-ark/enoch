from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for source in (
    ROOT / "src",
    ROOT / "libraries" / "provider-kit" / "src",
    ROOT / "libraries" / "github" / "src",
):
    sys.path.insert(0, str(source))

from our_ark_github import ENOCH_PROVIDERS, GithubForgeProvider


class GithubProviderTests(unittest.TestCase):
    def test_descriptor_registers_forge_factory(self) -> None:
        descriptor = ENOCH_PROVIDERS[0]

        self.assertEqual(descriptor["kind"], "forge")
        self.assertEqual(descriptor["name"], "github")
        self.assertTrue(descriptor["default"])

    @patch("our_ark_github.subprocess.run")
    def test_reads_published_text_through_forge_contract(self, run) -> None:
        encoded = base64.b64encode(b"name: Lucy\n").decode("ascii")
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"content": encoded})
        run.return_value.stderr = ""
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")

        text = provider.read_text("our-ark/lucy", "src/lucy/identity.yaml", "main")

        self.assertEqual(text, "name: Lucy\n")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/local/bin/gh",
                "api",
                "repos/our-ark/lucy/contents/src/lucy/identity.yaml?ref=main",
            ],
        )


if __name__ == "__main__":
    unittest.main()
