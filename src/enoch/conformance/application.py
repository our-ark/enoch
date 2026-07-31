from __future__ import annotations

from pathlib import Path

from enoch.application import (
    APPLICATION_COMPOSITION_API_VERSION,
    ApplicationComposition,
)
from enoch.identity import Identity


class ApplicationCompositionConformanceMixin:
    """Reusable structural checks for descendant application compositions."""

    def create_composition(self) -> ApplicationComposition:
        raise NotImplementedError

    def test_conformance_application_composition_uses_public_api_version(
        self,
    ) -> None:
        composition = self.create_composition()

        self.assertIsInstance(composition, ApplicationComposition)
        self.assertEqual(
            composition.api_version,
            APPLICATION_COMPOSITION_API_VERSION,
        )
        self.assertTrue(composition.name)

    def test_conformance_application_composition_owns_identity_boundary(
        self,
    ) -> None:
        composition = self.create_composition()
        root = Path.cwd().resolve()
        identity_path = Path(
            composition.identity_path_resolver(root)
        ).resolve()
        identity = composition.identity_loader(identity_path)

        self.assertIsInstance(identity, Identity)
        self.assertEqual(identity_path, root / identity_path.relative_to(root))
        self.assertTrue(
            composition.presentation.resolved_display_name(identity)
        )
        self.assertTrue(
            composition.presentation.resolved_ready_message(identity)
        )


__all__ = ["ApplicationCompositionConformanceMixin"]
