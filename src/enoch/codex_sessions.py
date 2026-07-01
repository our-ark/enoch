from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 1
PROMPT_VERSION = 3


@dataclass(frozen=True)
class CodexSessionState:
    key: str
    session_id: str
    turn_count: int
    created_at: str
    updated_at: str
    prompt_version: int = PROMPT_VERSION


def codex_sessions_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "codex_sessions.json"


def load_codex_session(key: str, root: Path | None = None) -> CodexSessionState | None:
    data = _load_sessions(root)
    raw = data.get("sessions", {}).get(key)
    if not isinstance(raw, dict):
        return None
    session_id = str(raw.get("session_id") or "").strip()
    if not session_id:
        return None
    if _int(raw.get("prompt_version")) != PROMPT_VERSION:
        return None
    return CodexSessionState(
        key=key,
        session_id=session_id,
        turn_count=_int(raw.get("turn_count")),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        prompt_version=PROMPT_VERSION,
    )


def save_codex_session(state: CodexSessionState, root: Path | None = None) -> CodexSessionState:
    data = _load_sessions(root)
    sessions = data.setdefault("sessions", {})
    sessions[state.key] = {
        "session_id": state.session_id,
        "turn_count": state.turn_count,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "prompt_version": PROMPT_VERSION,
    }
    _write_sessions(data, root)
    return state


def forget_codex_session(key: str, root: Path | None = None) -> None:
    data = _load_sessions(root)
    sessions = data.setdefault("sessions", {})
    if key in sessions:
        del sessions[key]
        _write_sessions(data, root)


def record_codex_session_turn(
    key: str,
    session_id: str,
    root: Path | None = None,
    *,
    previous: CodexSessionState | None = None,
) -> CodexSessionState:
    timestamp = current_time()
    turn_count = (previous.turn_count if previous else 0) + 1
    state = CodexSessionState(
        key=key,
        session_id=session_id,
        turn_count=turn_count,
        created_at=previous.created_at if previous and previous.created_at else timestamp,
        updated_at=timestamp,
        prompt_version=PROMPT_VERSION,
    )
    return save_codex_session(state, root)


def _load_sessions(root: Path | None = None) -> dict:
    path = codex_sessions_path(root)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "sessions": {}}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    return {"schema_version": SCHEMA_VERSION, "sessions": sessions}


def _write_sessions(data: dict, root: Path | None = None) -> None:
    path = codex_sessions_path(root)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sessions": data.get("sessions") if isinstance(data.get("sessions"), dict) else {},
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0
