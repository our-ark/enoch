"""Read Claude Code's structured /usage data, without inference or token access.

get_usage is an experimental CLI control request (verified with 2.1.258).
Older CLIs may reject it; never fall back to sending /usage as a model prompt.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess


def read_quota(executable: str, root: Path | None = None, *, timeout: float = 20) -> dict:
    command = [
        executable, "--print", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--settings", '{"disableAllHooks":true}', "--setting-sources", "",
    ]
    requests = (
        {"type": "control_request", "request_id": "quota-init", "request": {"subtype": "initialize"}},
        {"type": "control_request", "request_id": "quota-read", "request": {"subtype": "get_usage"}},
    )
    unavailable = {"error": "Quota unavailable. Check Claude Code login and update the CLI if get_usage is unsupported."}
    try:
        result = subprocess.run(
            command, input="".join(json.dumps(request) + "\n" for request in requests),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=root, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Quota query timed out. Try again later."}
    except OSError:
        return unavailable
    if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024:
        return unavailable
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("type") != "control_response":
            continue
        response = message.get("response")
        if not isinstance(response, dict) or response.get("request_id") != "quota-read":
            continue
        payload = response.get("response")
        if response.get("subtype") != "success" or not isinstance(payload, dict):
            return unavailable
        return parse_usage(payload)
    return unavailable


def parse_usage(payload: dict) -> dict:
    snapshot = {"plan": payload.get("subscription_type"),
                "source": "Claude Code get_usage (experimental)", "windows": []}
    limits = payload.get("rate_limits")
    if payload.get("rate_limits_available") is False:
        snapshot["error"] = "Plan quota is unavailable for this authentication mode (subscription login with profile access required)."
        return snapshot
    if not isinstance(limits, dict):
        return snapshot
    labels = {"five_hour": "5h", "seven_day": "7d", "seven_day_oauth_apps": "7d / OAuth apps",
              "seven_day_opus": "7d / Opus", "seven_day_sonnet": "7d / Sonnet"}
    for key, label in labels.items():
        window = limits.get(key)
        if isinstance(window, dict):
            snapshot["windows"].append({"label": label, "used_percent": window.get("utilization"),
                                        "resets_at": window.get("resets_at")})
    model_windows = limits.get("model_scoped")
    if isinstance(model_windows, list):
        for window in model_windows:
            if isinstance(window, dict):
                snapshot["windows"].append({"label": f"7d / {window.get('display_name', 'model')}",
                                            "used_percent": window.get("utilization"),
                                            "resets_at": window.get("resets_at")})
    return snapshot
