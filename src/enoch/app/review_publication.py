from pathlib import Path

from enoch.app.parsing import existing_branch_publish_request
from enoch.config import read_section
from enoch.providers.contracts import ReviewProvider, ReviewRecord


def requires_remote_review(root: Path, provider: ReviewProvider, request: str = "") -> bool:
    # A configured remote provider must never silently downgrade to local capture.
    configured = read_section("task", root).get("require_remote_review", "").strip().lower()
    configured_required = configured not in {"", "false", "no", "off", "0"}
    return (
        bool(getattr(provider, "supports_remote_review", True))
        or existing_branch_publish_request(request) is not None
        or configured_required
    )


def review_was_published(review: ReviewRecord) -> bool:
    return bool(review.identity.id) and review.state in {"open", "published"}


def review_publication_problem(
    review: ReviewRecord, provider: ReviewProvider, root: Path, request: str = "",
) -> str:
    if not requires_remote_review(root, provider, request):
        return ""
    if not getattr(provider, "supports_remote_review", True):
        return (
            "This instance requires a remote review, but its review provider is local-only. "
            "Configure and authenticate a remote forge before retrying publication."
        )
    if not review_was_published(review) or not review.identity.url:
        return "The review provider did not confirm an open or published review with a review URL."
    return ""
