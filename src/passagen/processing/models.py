"""Adapter-independent batch processing runs (roadmap: Web processing UI M1)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

RunMode = Literal["continue", "rebuild"]


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


class ProcessingError(RuntimeError):
    """Base class for stable processing-run failures exposed to adapters."""


class RunConflictError(ProcessingError):
    """A paper already belongs to an active (queued/running) run."""


class RunNotFoundError(ProcessingError):
    pass


class RunNotQueuedError(ProcessingError):
    """The run cannot be executed because it is no longer queued."""


class UnknownPaperError(ProcessingError):
    """One or more requested papers do not exist."""

    def __init__(self, paper_ids: list[str]) -> None:
        self.paper_ids = paper_ids
        super().__init__(f"Paper not found: {', '.join(paper_ids)}")


class RunPaperFailure(BaseModel):
    paper_id: str
    category: str
    message: str


class RunResultSummary(BaseModel):
    updated: list[str] = []
    skipped: list[str] = []
    failed: list[RunPaperFailure] = []
    warnings: list[RunPaperFailure] = []


class ProcessingRun(BaseModel):
    id: str
    paper_ids: list[str]
    mode: RunMode
    from_stage: str | None = None
    status: RunStatus
    current_paper_id: str | None = None
    current_stage: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: RunResultSummary | None = None


class ProgressEvent(BaseModel):
    sequence: int
    run_id: str
    paper_id: str | None
    stage: str
    current: int
    total: int
    message: str
