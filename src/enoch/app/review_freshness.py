"""Whether a review head already contains the current tip of its base branch.

Forges recompute their merge status lazily. Right after the base moves (for
example when another review lands), GitHub can still report UNSTABLE or UNKNOWN
for a review that is really behind, together with the failed checks of its old
head. Those checks say nothing about the review once it is updated.

So red CI only counts against a review when the failed head already contains
the base tip. This module is the one place that answers that question, for
pending merges, CI repair and status displays alike. It is provider-agnostic:

- ``forge.base-freshness`` (``inspect_review_base_freshness``) is preferred;
  the forge compares the head with the base tip it reads itself.
- Otherwise ``forge.ci-repair``'s ``compare_review_revisions`` is asked whether
  the head descends from the base branch.
- Forges with neither return ``None`` (cannot tell); callers keep their
  previous behavior.

Callers authorize the returned capability before asking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enoch.providers.contracts import (
    ReviewBaseFreshness,
    ReviewIdentity,
    ReviewProviderError,
    ReviewRevisionComparison,
)


BASE_FRESHNESS_CAPABILITY = "forge.base-freshness"
CI_REPAIR_CAPABILITY = "forge.ci-repair"


def base_tip_capability(review: Any) -> str | None:
    """The capability ``head_contains_base_tip`` would use on ``review``, if any."""

    if _supports(review, BASE_FRESHNESS_CAPABILITY, "inspect_review_base_freshness"):
        return BASE_FRESHNESS_CAPABILITY
    if _supports(review, CI_REPAIR_CAPABILITY, "compare_review_revisions"):
        return CI_REPAIR_CAPABILITY
    return None


def head_contains_base_tip(
    review: Any,
    identity: ReviewIdentity,
    *,
    head: str,
    base_branch: str = "",
    root: Path | None = None,
) -> bool | None:
    """True when ``head`` contains the base tip, False when it is behind it.

    ``None`` means this forge cannot tell. Forge errors propagate, so a caller
    can keep waiting instead of acting on an answer it does not have.
    """

    head = head.strip()
    if not head:
        return None
    capability = base_tip_capability(review)
    if capability == BASE_FRESHNESS_CAPABILITY:
        freshness = review.inspect_review_base_freshness(identity, revision=head, root=root)
        if not isinstance(freshness, ReviewBaseFreshness) or freshness.revision != head:
            raise ReviewProviderError("Forge returned invalid base freshness data.")
        return freshness.contains_base
    if capability == CI_REPAIR_CAPABILITY and base_branch.strip():
        comparison = review.compare_review_revisions(
            identity,
            base=base_branch.strip(),
            revision=head,
            root=root,
        )
        if not isinstance(comparison, ReviewRevisionComparison):
            raise ReviewProviderError("Forge returned invalid revision comparison data.")
        return comparison.descends
    return None


def _supports(review: Any, capability: str, method: str) -> bool:
    declared = getattr(review, "capabilities", None)
    if callable(declared):
        declared = declared()
    supports = getattr(declared, "supports", None)
    return (
        callable(supports)
        and bool(supports(capability))
        and callable(getattr(review, method, None))
    )
