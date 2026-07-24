from __future__ import annotations

from contextvars import ContextVar

from enoch.app.models import WorkStatusMessage
from enoch.prompt_append import TaskRegressionSignal


CURRENT_WORK_STATUS: ContextVar[WorkStatusMessage | None] = ContextVar(
    "enoch_work_status",
    default=None,
)
CURRENT_TASK_ID: ContextVar[int | None] = ContextVar("enoch_task_id", default=None)
CURRENT_TASK_WORKER_ID: ContextVar[str] = ContextVar(
    "enoch_task_worker_id",
    default="",
)
CURRENT_REGRESSION_SIGNALS: ContextVar[tuple[TaskRegressionSignal, ...]] = ContextVar(
    "enoch_regression_signals",
    default=(),
)
