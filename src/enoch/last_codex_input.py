from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from enoch.memory.paths import atomic_write, now
from enoch.paths import enoch_home
from enoch.state import load_json_object


def last_codex_input_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "last_codex_input.json"


def record_last_codex_input(
    prompt: str,
    root: Path | None = None,
    *,
    sandbox: str,
    persist_session: bool,
    session_id: str,
    resumed: bool,
) -> None:
    payload = {
        "recorded_at": now(),
        "sandbox": sandbox,
        "persist_session": persist_session,
        "session_id": session_id,
        "resumed": resumed,
        "prompt": prompt,
    }
    atomic_write(last_codex_input_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


def last_codex_input_message(root: Path | None = None) -> str:
    data = _load_last_codex_input(root)
    if data is None:
        return "No Codex input has been recorded yet."

    prompt = str(data.get("prompt") or "")
    lines = [
        "Last Codex input:",
        f"- recorded at: {data.get('recorded_at') or 'unknown'}",
        f"- sandbox: {data.get('sandbox') or 'unknown'}",
        f"- persistent session: {_yes_no(data.get('persist_session'))}",
        f"- resumed session: {_yes_no(data.get('resumed'))}",
    ]
    session_id = str(data.get("session_id") or "").strip()
    if session_id:
        lines.append(f"- session id: {session_id}")
    if data.get("resumed"):
        lines.extend(
            [
                "",
                "Note: this is the exact new input Enoch sent to Codex. The token count can also include Codex-managed context from the resumed session.",
            ]
        )
    lines.extend(["", "Input payload:", "```text", prompt, "```"])
    return "\n".join(lines)


def _load_last_codex_input(root: Path | None = None) -> dict[str, Any] | None:
    path = last_codex_input_path(root)
    if not path.exists():
        return None
    return load_json_object(path)


def _yes_no(value: object) -> str:
    return "yes" if bool(value) else "no"
