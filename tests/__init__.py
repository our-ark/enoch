"""Test-suite state isolation.

Tests that intentionally use the source checkout as their repository root must
never read or modify a developer's live `.enoch` daemon state.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
STATE_HOME = Path(tempfile.mkdtemp(prefix="enoch-test-state-"))

os.environ["ENOCH_STATE_REDIRECT_ROOT"] = str(SOURCE_ROOT)
os.environ["ENOCH_STATE_HOME"] = str(STATE_HOME)


_original_test_case_run = unittest.TestCase.run


def _run_with_clean_resident_state(
    test_case: unittest.TestCase,
    *args: object,
    **kwargs: object,
):
    shutil.rmtree(STATE_HOME, ignore_errors=True)
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    return _original_test_case_run(test_case, *args, **kwargs)


unittest.TestCase.run = _run_with_clean_resident_state

atexit.register(shutil.rmtree, STATE_HOME, ignore_errors=True)
