from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable

from enoch.app.epoch import DaemonEpoch, begin_daemon_epoch
from enoch.extensions import AgentExtension, AgentExtensionError, load_extensions
from enoch.identity import Identity, identity_file_path, load_identity
from enoch.profiles import AgentProfile, load_profile
from enoch.providers import (
    AgentRuntime,
    AuthorizationPolicy,
    ChatProvider,
    RepositoryProvider,
    ReviewProvider,
    as_repository_provider,
    as_review_provider,
)
from enoch.providers.registry import load_provider
from enoch.workflows import LocalWorkflowEngine, WorkflowEngine, validate_workflow_engine


APPLICATION_COMPOSITION_API_VERSION = 1
IdentityLoader = Callable[[Path], Identity]
IdentityPathResolver = Callable[[Path], Path]
WorkflowFactory = Callable[[Path, DaemonEpoch], WorkflowEngine]


class ApplicationCompositionError(RuntimeError):
    pass


def _load_default_identity(path: Path) -> Identity:
    """Prefer Enoch's mutable source identity, with wheel data as fallback."""

    return load_identity(path if path.is_file() else None)


@dataclass(frozen=True)
class ApplicationPresentation:
    """Bounded application-level strings owned by a descendant."""

    display_name: str = ""
    ready_message: str = ""

    def __post_init__(self) -> None:
        for field_name, limit in (("display_name", 80), ("ready_message", 240)):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ApplicationCompositionError(
                    f"Application presentation {field_name} must be a string."
                )
            value = value.strip()
            if "\n" in value or len(value) > limit:
                raise ApplicationCompositionError(
                    f"Application presentation {field_name} must be one line "
                    f"and {limit} characters or fewer."
                )
            object.__setattr__(self, field_name, value)

    def resolved_display_name(self, identity: Identity) -> str:
        return self.display_name or identity.name

    def resolved_ready_message(self, identity: Identity) -> str:
        return self.ready_message or f"{self.resolved_display_name(identity)} is ready."


@dataclass(frozen=True)
class ApplicationProviderSelection:
    chat: str = ""
    runtime: str = ""
    vcs: str = ""
    forge: str = ""

    def __post_init__(self) -> None:
        for field_name in ("chat", "runtime", "vcs", "forge"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ApplicationCompositionError(
                    f"Application provider selection {field_name} must be a string."
                )
            value = value.strip().lower()
            if value and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value):
                raise ApplicationCompositionError(
                    f"Invalid {field_name} provider name {value!r}."
                )
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class ApplicationComponents:
    composition_name: str
    identity: Identity
    identity_path: Path
    presentation: ApplicationPresentation
    chat: ChatProvider
    runtime: AgentRuntime
    repository: RepositoryProvider
    review: ReviewProvider
    profile: AgentProfile
    extensions: tuple[AgentExtension, ...]
    daemon_epoch: DaemonEpoch
    workflow: WorkflowEngine
    authorization_policy: AuthorizationPolicy | None = None


@dataclass(frozen=True)
class ApplicationComposition:
    """Versioned descendant-owned inputs to Enoch application startup."""

    name: str = "enoch"
    api_version: int = APPLICATION_COMPOSITION_API_VERSION
    identity_loader: IdentityLoader = _load_default_identity
    identity_path_resolver: IdentityPathResolver = identity_file_path
    presentation: ApplicationPresentation = field(
        default_factory=ApplicationPresentation
    )
    profile_name: str = ""
    required_extensions: tuple[str, ...] = ()
    include_configured_extensions: bool = True
    providers: ApplicationProviderSelection = field(
        default_factory=ApplicationProviderSelection
    )
    workflow_factory: WorkflowFactory | None = None
    authorization_policy: AuthorizationPolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise ApplicationCompositionError(
                "Application composition name must be a string."
            )
        name = self.name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            raise ApplicationCompositionError(
                f"Invalid application composition name {self.name!r}."
            )
        if self.api_version != APPLICATION_COMPOSITION_API_VERSION:
            raise ApplicationCompositionError(
                f"Application composition {name} uses API version "
                f"{self.api_version}; Enoch supports version "
                f"{APPLICATION_COMPOSITION_API_VERSION}."
            )
        if not isinstance(self.presentation, ApplicationPresentation):
            raise ApplicationCompositionError(
                "Application composition presentation must be "
                "ApplicationPresentation."
            )
        if not isinstance(self.providers, ApplicationProviderSelection):
            raise ApplicationCompositionError(
                "Application composition providers must be "
                "ApplicationProviderSelection."
            )
        if not callable(self.identity_loader):
            raise ApplicationCompositionError(
                "Application composition identity loader must be callable."
            )
        if not callable(self.identity_path_resolver):
            raise ApplicationCompositionError(
                "Application composition identity path resolver must be callable."
            )
        if self.workflow_factory is not None and not callable(self.workflow_factory):
            raise ApplicationCompositionError(
                "Application composition workflow factory must be callable."
            )
        if not isinstance(self.include_configured_extensions, bool):
            raise ApplicationCompositionError(
                "Application composition extension selection must be boolean."
            )
        if isinstance(self.required_extensions, str):
            raise ApplicationCompositionError(
                "Application composition required extensions must be a tuple."
            )
        try:
            required = tuple(
                AgentExtension(name=extension_name).name
                for extension_name in self.required_extensions
            )
        except (AgentExtensionError, TypeError) as error:
            raise ApplicationCompositionError(
                f"Invalid required application extension: {error}"
            ) from error
        if len(set(required)) != len(required):
            raise ApplicationCompositionError(
                "Application composition required extensions must be unique."
            )
        object.__setattr__(self, "name", name)
        if not isinstance(self.profile_name, str):
            raise ApplicationCompositionError(
                "Application composition profile name must be a string."
            )
        profile_name = self.profile_name.strip().lower()
        if profile_name and not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,63}",
            profile_name,
        ):
            raise ApplicationCompositionError(
                f"Invalid application profile name {self.profile_name!r}."
            )
        object.__setattr__(self, "profile_name", profile_name)
        object.__setattr__(self, "required_extensions", required)

    def resolve(
        self,
        root: Path,
        *,
        chat_provider_name: str = "",
    ) -> ApplicationComponents:
        resolved_root = Path(root).resolve()
        mutable_identity_path = Path(
            self.identity_path_resolver(resolved_root)
        ).resolve()
        try:
            mutable_identity_path.relative_to(resolved_root)
        except ValueError as error:
            raise ApplicationCompositionError(
                "Application mutable identity path must remain inside the "
                "instance root."
            ) from error

        try:
            identity = self.identity_loader(mutable_identity_path)
        except (KeyError, OSError, TypeError, ValueError) as error:
            raise ApplicationCompositionError(
                f"Application composition {self.name} could not load identity "
                f"from {mutable_identity_path}: {error}"
            ) from error
        if not isinstance(identity, Identity):
            raise ApplicationCompositionError(
                f"Application composition {self.name} identity loader did not "
                "return Identity."
            )

        chat_name = chat_provider_name.strip() or self.providers.chat
        chat = load_provider("chat", resolved_root, name=chat_name)
        runtime = load_provider(
            "runtime",
            resolved_root,
            name=self.providers.runtime,
        )
        repository = as_repository_provider(
            load_provider("vcs", resolved_root, name=self.providers.vcs)
        )
        review = as_review_provider(
            load_provider("forge", resolved_root, name=self.providers.forge)
        )
        profile = load_profile(resolved_root, name=self.profile_name)
        extensions = self._resolve_extensions(resolved_root)
        provider_name = str(getattr(chat, "name", "chat")).strip() or "chat"
        daemon_epoch = begin_daemon_epoch(
            resolved_root,
            provider=provider_name,
        )
        workflow = validate_workflow_engine(
            (
                self.workflow_factory(resolved_root, daemon_epoch)
                if self.workflow_factory is not None
                else LocalWorkflowEngine(resolved_root, epoch=daemon_epoch)
            )
        )
        return ApplicationComponents(
            composition_name=self.name,
            identity=identity,
            identity_path=mutable_identity_path,
            presentation=self.presentation,
            chat=chat,
            runtime=runtime,
            repository=repository,
            review=review,
            profile=profile,
            extensions=extensions,
            daemon_epoch=daemon_epoch,
            workflow=workflow,
            authorization_policy=self.authorization_policy,
        )

    def _resolve_extensions(self, root: Path) -> tuple[AgentExtension, ...]:
        configured = (
            load_extensions(root)
            if self.include_configured_extensions
            else ()
        )
        configured_by_name = {
            extension.name: extension
            for extension in configured
        }
        required = []
        for name in self.required_extensions:
            extension = configured_by_name.get(name)
            if extension is None:
                extension = load_extensions(root, names=(name,))[0]
            required.append(extension)
        required_names = set(self.required_extensions)
        return (
            *required,
            *(
                extension
                for extension in configured
                if extension.name not in required_names
            ),
        )


def run_application(
    composition: ApplicationComposition,
    *,
    chat_provider_name: str = "",
) -> None:
    """Run a composed descendant through Enoch's owned lifecycle."""

    from enoch.app.core import main

    main(
        chat_provider_name=chat_provider_name,
        composition=composition,
    )


__all__ = [
    "APPLICATION_COMPOSITION_API_VERSION",
    "ApplicationComponents",
    "ApplicationComposition",
    "ApplicationCompositionError",
    "ApplicationPresentation",
    "ApplicationProviderSelection",
    "IdentityLoader",
    "IdentityPathResolver",
    "WorkflowFactory",
    "run_application",
]
