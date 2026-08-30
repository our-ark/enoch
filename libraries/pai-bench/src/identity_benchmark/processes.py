from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
from typing import Mapping, Sequence


_CLEANUP_TIMEOUT_SECONDS = 2.0


def run_text_command(
    command: Sequence[str],
    *,
    input_text: str,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a text command with a deadline that also terminates descendants."""
    kwargs: dict[str, object] = {}
    if os.name == "posix":
        # A dedicated session makes the child a process-group leader. Killing
        # that group on timeout also closes pipes inherited by grandchildren.
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        tuple(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=cwd,
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(
            input=input_text,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        stdout, stderr = _finish_terminated_process(process, error)
        raise subprocess.TimeoutExpired(
            tuple(command),
            timeout_seconds,
            output=stdout,
            stderr=stderr,
        ) from error
    return subprocess.CompletedProcess(
        args=tuple(command),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _finish_terminated_process(
    process: subprocess.Popen[str],
    timeout: subprocess.TimeoutExpired,
) -> tuple[str, str]:
    try:
        return process.communicate(timeout=_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Never replace one hung command with a hung timeout cleanup. Closing
        # our pipe ends prevents a detached descendant from retaining them.
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        return _timeout_text(timeout.output), _timeout_text(timeout.stderr)


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
