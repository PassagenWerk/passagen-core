"""Adapter-independent processing service and persisted run contract."""

from passagen.processing.models import (
    ACTIVE_RUN_STATUSES,
    ProcessingError,
    ProcessingRun,
    ProgressEvent,
    RunConflictError,
    RunMode,
    RunNotFoundError,
    RunNotQueuedError,
    RunPaperFailure,
    RunResultSummary,
    RunStatus,
    UnknownPaperError,
)
from passagen.processing.service import ProcessingService

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ProcessingError",
    "ProcessingRun",
    "ProcessingService",
    "ProgressEvent",
    "RunConflictError",
    "RunMode",
    "RunNotFoundError",
    "RunNotQueuedError",
    "RunPaperFailure",
    "RunResultSummary",
    "RunStatus",
    "UnknownPaperError",
]
