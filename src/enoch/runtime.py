from __future__ import annotations


DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
PROTECTED_BRANCHES = {DEFAULT_BRANCH, "master"}

ACTION_SANDBOX_READ_ONLY = "read-only"
ACTION_SANDBOX_FULL_ACCESS = "danger-full-access"
WORKSPACE_WRITE_SANDBOX = "workspace-write"

DEFAULT_DAEMON_LOG_LINES = 80
