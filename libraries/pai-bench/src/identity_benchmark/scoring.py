from __future__ import annotations

import re
import unicodedata
from typing import Protocol

from identity_benchmark.contracts import Expectation, ExpectationResult


class ExpectationScorer(Protocol):
    """Pluggable scorer for one observable expectation."""

    def score(self, response: str, expectation: Expectation) -> ExpectationResult: ...


class DeterministicScorer:
    """Unicode-normalized exact, substring, exclusion, and regex scoring."""

    def score(self, response: str, expectation: Expectation) -> ExpectationResult:
        if expectation.type == "regex":
            passed = re.search(
                expectation.value,
                response,
                flags=re.IGNORECASE | re.MULTILINE,
            ) is not None
        else:
            actual = normalize_text(response)
            expected = normalize_text(expectation.value)
            if expectation.type == "exact":
                passed = actual == expected
            elif expectation.type == "contains":
                passed = expected in actual
            else:
                passed = expected not in actual
        return ExpectationResult(expectation=expectation, passed=passed)


def weighted_expectation_score(results: tuple[ExpectationResult, ...]) -> float:
    if any(result.expectation.gate and not result.passed for result in results):
        return 0.0
    total = sum(result.expectation.weight for result in results)
    if total == 0:
        return 0.0
    earned = sum(
        result.expectation.weight for result in results if result.passed
    )
    return earned / total


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())
