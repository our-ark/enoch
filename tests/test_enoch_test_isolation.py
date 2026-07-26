from __future__ import annotations

import os
import sys
import unittest


class EnochTestIsolationTests(unittest.TestCase):
    def test_canonical_discovery_loads_test_state_isolation(self) -> None:
        self.assertEqual(
            __package__,
            "tests",
            "run unittest discovery with '-t .' so tests/__init__.py is loaded",
        )
        isolation = sys.modules.get("tests")
        self.assertIsNotNone(isolation)
        state_home = isolation.STATE_HOME
        self.assertEqual(os.environ.get("ENOCH_STATE_HOME"), str(state_home))
        self.assertIs(unittest.TestCase.run, isolation._run_with_clean_resident_state)


if __name__ == "__main__":
    unittest.main()
