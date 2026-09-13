"""Read account limits through Codex app-server without starting a model turn."""
from __future__ import annotations

import json
import math
import queue
from pathlib import Path
import subprocess
import threading
import time


def read_quota(root: Path | None = None, *, timeout: float = 20) -> dict | None:
    from enoch.brain import resolve_codex_executable

    executable = resolve_codex_executable(root).path
    if executable is None:
        return None
    try:
        payload = _read_limits(executable, root, timeout)
    except TimeoutError:
        return {"error": "Quota query timed out. Try again later."}
    except (OSError, ValueError):
        return {"error": "Quota unavailable. Check Codex CLI version and ChatGPT login on this host."}
    return parse_limits(payload)


def _read_limits(executable: str, root: Path | None, timeout: float) -> dict:
    # Codex must finish initialize before it receives account/rateLimits/read.
    # Keep stdin open until the reply: EOF can stop app-server before it replies.
    process = subprocess.Popen(
        [executable, "app-server", "--stdio"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    messages: queue.Queue = queue.Queue(maxsize=8)
    stopped = threading.Event()

    def read_lines():
        while not stopped.is_set():
            line = process.stdout.readline(1024 * 1024 + 1)
            while not stopped.is_set():
                try:
                    messages.put(line, timeout=0.1)
                    break
                except queue.Full:
                    pass
            if not line or len(line) > 1024 * 1024:
                return

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()

    def response(request_id):
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError from None
            if not line or len(line) > 1024 * 1024:
                raise ValueError("Missing or oversized response")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise ValueError("Quota request rejected")
            return message["result"]
        raise TimeoutError

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "enoch_quota", "version": "1.0.0"},
        }})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        return response(2)
    finally:
        stopped.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()


def parse_limits(payload: dict) -> dict:
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        buckets = tuple(by_id.items())
    else:
        buckets = (("codex", payload.get("rateLimits")),)
    windows = []
    plans = []
    for bucket_id, bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        if plan := bucket.get("planType"):
            if plan not in plans:
                plans.append(plan)
        label = bucket.get("limitName") or bucket.get("limitId") or bucket_id
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowDurationMins")
            duration = key
            if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and math.isfinite(minutes) and minutes > 0:
                duration = f"{minutes / 1440:g}d" if minutes % 1440 == 0 else (
                    f"{minutes / 60:g}h" if minutes % 60 == 0 else f"{minutes:g}m"
                )
            windows.append({"label": f"{label} / {duration}",
                            "used_percent": window.get("usedPercent"),
                            "resets_at": window.get("resetsAt")})
    return {"windows": windows, "plan": ", ".join(str(plan) for plan in plans),
            "source": "Codex account/rateLimits/read"}
