from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.version_status import format_code_version_status


class _VersionControl:
    authoritative_revision_source = "cached remote ref"

    def __init__(
        self,
        *,
        local: str = "",
        authoritative: str = "",
        ancestors: set[tuple[str, str]] | None = None,
    ) -> None:
        self.local = local
        self.authoritative = authoritative
        self.ancestors = ancestors or set()
        self.refreshed = False

    def current_revision(self, root=None):
        return self.local

    def authoritative_revision(self, root=None):
        return self.authoritative

    def is_ancestor(self, revision, descendant, root=None):
        return (revision, descendant) in self.ancestors

    def refresh_authoritative(self, root=None):
        self.refreshed = True
        raise AssertionError("status must not refresh the authoritative revision")


def _status(
    *,
    running: str,
    local: str,
    authoritative: str,
    ancestors: set[tuple[str, str]] | None = None,
) -> tuple[str, _VersionControl]:
    provider = _VersionControl(
        local=local,
        authoritative=authoritative,
        ancestors=ancestors,
    )
    output = format_code_version_status(
        ROOT,
        "telegram",
        lifecycle_loader=lambda _channel, _root: {"started_head": running},
        vcs_loader=lambda _kind, _root: provider,
    )
    return output, provider


class EnochVersionStatusTests(unittest.TestCase):
    def test_all_revisions_equal_is_current(self) -> None:
        revision = "1111111111111111111111111111111111111111"

        output, provider = _status(
            running=revision,
            local=revision,
            authoritative=revision,
        )

        self.assertIn("- Running: 1111111", output)
        self.assertIn("- Local: 1111111", output)
        self.assertIn("- Authoritative (cached remote ref): 1111111", output)
        self.assertIn("- State: current", output)
        self.assertFalse(provider.refreshed)

    def test_older_running_revision_needs_restart(self) -> None:
        current = "2222222222222222222222222222222222222222"

        output, _provider = _status(
            running="1111111111111111111111111111111111111111",
            local=current,
            authoritative=current,
        )

        self.assertIn("- State: restart needed", output)

    def test_authoritative_revision_ahead_of_local_has_update(self) -> None:
        local = "1111111111111111111111111111111111111111"
        authoritative = "2222222222222222222222222222222222222222"

        output, _provider = _status(
            running=local,
            local=local,
            authoritative=authoritative,
            ancestors={(local, authoritative)},
        )

        self.assertIn("- State: update available", output)

    def test_local_revision_ahead_is_not_published(self) -> None:
        authoritative = "1111111111111111111111111111111111111111"
        local = "2222222222222222222222222222222222222222"

        output, _provider = _status(
            running=local,
            local=local,
            authoritative=authoritative,
            ancestors={(authoritative, local)},
        )

        self.assertIn("- State: local changes not published", output)

    def test_unavailable_revision_data_is_reported_without_error(self) -> None:
        output = format_code_version_status(
            ROOT,
            "telegram",
            lifecycle_loader=lambda _channel, _root: {},
            vcs_loader=lambda _kind, _root: (_ for _ in ()).throw(
                RuntimeError("not a repository")
            ),
        )

        self.assertIn("- Running: unavailable", output)
        self.assertIn("- Local: unavailable", output)
        self.assertIn("- Authoritative: unavailable", output)
        self.assertIn("- State: comparison unavailable", output)


if __name__ == "__main__":
    unittest.main()
