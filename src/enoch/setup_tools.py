from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from enoch.config import config_path, read_section, write_section_value
from enoch.lineage.core import LINEAGE_PATH, ParentLink, load_parent, parse_lineage_parent
from enoch.runtime import DEFAULT_TELEGRAM_POLL_TIMEOUT


PromptFn = Callable[[str], str]


def setup_command(
    text: str,
    root: Path,
    *,
    prompt: PromptFn | None = None,
    prefix: str = "",
) -> str:
    prompt_fn = prompt or input
    parts = text.split(maxsplit=2)
    command = parts[0].lower() if parts else "setup"
    if command == "setup-chat":
        argument = parts[1].strip() if len(parts) >= 2 else ""
        return save_chat_id(argument, root, prefix=prefix)
    if command == "setup-token":
        argument = parts[1].strip() if len(parts) >= 2 else ""
        if not argument:
            argument = prompt_fn("Telegram bot token: ").strip()
        return save_bot_token(argument, root, prefix=prefix)

    subcommand = parts[1].lower() if len(parts) >= 2 else ""
    argument = parts[2].strip() if len(parts) >= 3 else ""
    if not subcommand:
        return interactive_setup(root, prompt=prompt_fn, prefix=prefix)
    if subcommand in {"help", "-h", "--help"}:
        return setup_usage(prefix=prefix)
    if subcommand in {"show", "status"}:
        return setup_status(root, prefix=prefix)
    if subcommand in {"token", "bot-token"}:
        if not argument:
            argument = prompt_fn("Telegram bot token: ").strip()
        return save_bot_token(argument, root, prefix=prefix)
    if subcommand in {"chat", "chat-id", "allowed-chat-id"}:
        return save_chat_id(argument, root, prefix=prefix)
    if subcommand in {"poll", "poll-timeout", "timeout"}:
        return save_poll_timeout(argument, root, prefix=prefix)
    if subcommand in {"ancestor", "parent", "lineage"}:
        return ancestor_setup_command(argument, root, prefix=prefix)
    return setup_usage(prefix=prefix)


def interactive_setup(root: Path, *, prompt: PromptFn | None = None, prefix: str = "") -> str:
    prompt_fn = prompt or input
    settings = read_section("telegram", root)
    token = settings.get("bot_token", "").strip()
    messages: list[str] = []
    if token:
        messages.append("Telegram bot token is already saved.")
    else:
        token = prompt_fn("Telegram bot token: ").strip()
        saved = write_bot_token(token, root, prefix=prefix)
        messages.append(saved)
    messages.append(setup_status(root, prefix=prefix))
    return "\n\n".join(message for message in messages if message)


def save_bot_token(token: str, root: Path, *, prefix: str = "") -> str:
    saved = write_bot_token(token, root, prefix=prefix)
    if not token.strip():
        return saved
    return "\n".join([saved, _next_step_message(root, prefix=prefix)])


def write_bot_token(token: str, root: Path, *, prefix: str = "") -> str:
    token = token.strip()
    if not token:
        return f"Telegram bot token was not saved. Use {_command(prefix, 'setup-token <token>')}."
    write_section_value("telegram", "bot_token", token, root)
    return f"Telegram bot token saved to {config_path(root)}."


def save_chat_id(chat_id: str, root: Path, *, prefix: str = "") -> str:
    chat_id = chat_id.strip()
    if not chat_id:
        return f"Use {_command(prefix, 'setup-chat <chat_id>')}."
    try:
        int(chat_id)
    except ValueError:
        return "Telegram chat id must be a whole number."
    write_section_value("telegram", "allowed_chat_id", chat_id, root)
    return "\n".join(
        [
            f"Telegram chat lock saved to {config_path(root)}.",
            "Restart Enoch so the daemon uses the new chat lock:",
            "bin/enoch-daemon restart",
        ]
    )


def save_poll_timeout(seconds: str, root: Path, *, prefix: str = "") -> str:
    seconds = seconds.strip()
    if not seconds:
        return f"Use {_command(prefix, 'setup poll-timeout <seconds>')}."
    try:
        value = int(seconds)
    except ValueError:
        return "Telegram poll timeout must be a whole number of seconds."
    if value < 1:
        return "Telegram poll timeout must be at least 1 second."
    write_section_value("telegram", "poll_timeout", str(value), root)
    return f"Telegram poll timeout saved to {config_path(root)}."


def setup_status(root: Path, *, prefix: str = "") -> str:
    settings = read_section("telegram", root)
    token = settings.get("bot_token", "").strip()
    chat_id = settings.get("allowed_chat_id", "").strip()
    poll_timeout = settings.get("poll_timeout", "").strip() or str(DEFAULT_TELEGRAM_POLL_TIMEOUT)
    parent = load_parent(root)
    lines = [
        "Enoch setup status:",
        f"- config: {config_path(root)}",
        f"- Telegram bot token: {'saved' if token else 'missing'}",
        f"- Telegram chat lock: {chat_id if chat_id else 'not set'}",
        f"- Telegram poll timeout: {poll_timeout}",
        f"- lineage parent: {_parent_status(parent)}",
        "",
        _next_step_message(root, prefix=prefix),
    ]
    return "\n".join(lines)


def setup_usage(prefix: str = "") -> str:
    setup = _command(prefix, "setup")
    return "\n".join(
        [
            "Enoch setup commands:",
            f"{setup} - run first-time Telegram setup",
            f"{setup} show - show setup status without printing secrets",
            f"{setup} token <token> - save the Telegram bot token",
            f"{setup} chat <chat_id> - lock Enoch to one Telegram chat",
            f"{setup} poll-timeout <seconds> - set Telegram polling timeout",
            f"{setup} ancestor <github-url> - set the repo-side lineage parent",
            f"{setup} ancestor show - show lineage parent",
            f"{setup} ancestor clear - remove lineage parent",
            f"{_command(prefix, 'setup-token <token>')} - shortcut for {setup} token",
            f"{_command(prefix, 'setup-chat <chat_id>')} - shortcut for {setup} chat",
        ]
    )


def _next_step_message(root: Path, *, prefix: str = "") -> str:
    settings = read_section("telegram", root)
    token = settings.get("bot_token", "").strip()
    chat_id = settings.get("allowed_chat_id", "").strip()
    if not token:
        return f"Next: run {_command(prefix, 'setup-token <token>')}."
    if not chat_id:
        return "\n".join(
            [
                "Next:",
                "1. Start Enoch: bin/enoch-daemon start",
                "2. Send /status to the Telegram bot.",
                f"3. Copy the chat id, then run: {_command(prefix, 'setup-chat <chat_id>')}",
            ]
        )
    return "Setup is ready. Start or restart Enoch with: bin/enoch-daemon restart"


def ancestor_setup_command(argument: str, root: Path, *, prefix: str = "") -> str:
    parts = argument.split()
    action = parts[0].lower() if parts else ""
    if action in {"show", "status"}:
        parent = load_parent(root)
        if parent is None:
            return f"No lineage parent configured. Use {_command(prefix, 'setup ancestor <github-url>')}."
        return f"Lineage parent: {parent.name} ({parent.repo}@{parent.branch})"
    if action in {"clear", "remove", "none"}:
        path = root / LINEAGE_PATH
        try:
            path.unlink()
        except FileNotFoundError:
            return "No lineage parent was configured."
        return f"Removed lineage parent from {path}."
    spec = _parse_ancestor_link(parts)
    if spec is None:
        return f"Use {_command(prefix, 'setup ancestor <github-url>')}."
    return save_ancestor(spec.name, spec.repo, spec.branch, root)


def save_ancestor(name: str, repo: str, branch: str, root: Path) -> str:
    name = name.strip()
    repo = repo.strip()
    branch = branch.strip() or "main"
    if not name or not repo:
        return "Lineage parent needs both a name and repo."
    text = "\n".join(
        [
            "parent:",
            f"  name: {name}",
            f"  repo: {repo}",
            f"  branch: {branch}",
            "",
        ]
    )
    parent = parse_lineage_parent(text)
    if parent is None:
        return "Enoch could not parse that lineage parent."
    path = root / LINEAGE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "\n".join(
        [
            f"Lineage parent saved to {path}.",
            f"Parent: {parent.name} ({parent.repo}@{parent.branch})",
            "This is repo-side lineage metadata. Commit it with the descendant agent.",
        ]
    )


class _AncestorSpec:
    def __init__(self, name: str, repo: str, branch: str) -> None:
        self.name = name
        self.repo = repo
        self.branch = branch


def _parse_ancestor_link(parts: list[str]) -> _AncestorSpec | None:
    if len(parts) != 1:
        return None
    link = parts[0]
    if not _is_github_url(link):
        return None
    repo = _normalize_repo(link)
    if not repo:
        return None
    return _AncestorSpec(name=_infer_ancestor_name(repo), repo=repo, branch="main")


def _is_github_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "github.com"


def _normalize_repo(value: str) -> str:
    value = value.strip().removesuffix(".git")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.netloc:
        segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) >= 2:
            return f"{segments[0]}/{segments[1].removesuffix('.git')}"
        return ""
    if value.startswith("git@github.com:"):
        value = value.split(":", 1)[1]
    segments = [segment for segment in value.strip("/").split("/") if segment]
    if len(segments) == 2:
        return f"{segments[0]}/{segments[1]}"
    return ""


def _infer_ancestor_name(repo: str) -> str:
    repo_name = repo.split("/", 1)[-1]
    words = [word for word in repo_name.replace("_", "-").split("-") if word]
    if not words:
        return repo_name or "Ancestor"
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _parent_status(parent: ParentLink | None) -> str:
    if parent is None:
        return "not set"
    return f"{parent.name} ({parent.repo}@{parent.branch})"


def _command(prefix: str, text: str) -> str:
    if prefix:
        return f"{prefix}{text}"
    return f"bin/enoch {text}"
