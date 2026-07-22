from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "github" / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_github.workflow import feature_title


class EnochGithubTitleTests(unittest.TestCase):
    def test_feature_title_normalizes_and_clips_request_text(self) -> None:
        title = feature_title("  update   the README " + "with details " * 20)

        self.assertTrue(title.startswith("update the README with details"))
        self.assertLessEqual(len(title), 72)

    def test_feature_title_has_fallback(self) -> None:
        self.assertEqual(feature_title("   "), "Agent feature")


if __name__ == "__main__":
    unittest.main()
