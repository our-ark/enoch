from __future__ import annotations

import json
from pathlib import Path
import secrets

from enoch.config import read_section
from enoch.paths import private_state_path
from enoch.state import atomic_write, load_json_object


DEFAULT_LOCAL_WEB_HOST = "127.0.0.1"
DEFAULT_LOCAL_WEB_PORT = 36624
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


class LocalWebSettings:
    def __init__(
        self,
        *,
        enabled: bool,
        host: str,
        port: int,
        token: str,
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.port = port
        self.token = token

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_local_web_settings(root: Path | None = None) -> LocalWebSettings:
    section = read_section("local_web", root)
    enabled = _truthy(section.get("enabled", "true"))
    host = (section.get("bind") or DEFAULT_LOCAL_WEB_HOST).strip() or DEFAULT_LOCAL_WEB_HOST
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"local_web.bind must be loopback ({DEFAULT_LOCAL_WEB_HOST}), not {host}."
        )
    port = _port(section.get("port"), DEFAULT_LOCAL_WEB_PORT)
    token = _ensure_token(root)
    return LocalWebSettings(enabled=enabled, host=host, port=port, token=token)


def local_web_page_url(
    shortlist_id: str = "",
    *,
    tab: int | None = None,
    root: Path | None = None,
    settings: LocalWebSettings | None = None,
    include_token: bool = True,
) -> str:
    resolved = settings or load_local_web_settings(root)
    path = "/shop"
    cleaned = shortlist_id.strip()
    if cleaned:
        path = f"/shop/{cleaned}"
        if tab is not None and tab > 0:
            path = f"{path}/{tab}"
    url = f"http://127.0.0.1:{resolved.port}{path}"
    if include_token and resolved.token:
        url = f"{url}?token={resolved.token}"
    return url


def attach_local_shop_page_link(
    text: str,
    page_url: str,
    *,
    shortlist_id: str = "",
    title: str = "",
) -> str:
    cleaned = page_url.strip()
    if not cleaned:
        return text
    label = "Latest shop page on this Mac"
    cleaned_id = shortlist_id.strip()
    if cleaned_id:
        label = f"Latest shop page ({cleaned_id})"
    snippet = " ".join(title.split())[:80]
    if snippet:
        label = f"{label}: {snippet}"
    return (
        f"{text.rstrip()}\n"
        "<!-- telegram:break -->\n\n"
        f"[{label}]({cleaned})"
    )


def _ensure_token(root: Path | None) -> str:
    path = _token_path(root)
    data = load_json_object(path) if path.exists() else {}
    token = str(data.get("token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(16)
        atomic_write(path, json.dumps({"token": token}, indent=2) + "\n")
    return token


def _token_path(root: Path | None) -> Path:
    return private_state_path("local_web.json", root)


def _port(value: str | None, default: int) -> int:
    if value is None or not str(value).strip():
        return default
    try:
        port = int(str(value).strip())
    except ValueError as error:
        raise ValueError("local_web.port must be an integer.") from error
    if port < 0 or port > 65535:
        raise ValueError("local_web.port must be between 0 and 65535.")
    return port


def _truthy(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}
