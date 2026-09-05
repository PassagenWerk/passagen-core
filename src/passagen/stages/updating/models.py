from collections.abc import Callable
from dataclasses import dataclass, field

from passagen.domain import PaperStatus
from passagen.storage.repository import PaperRecord

LATEST_IMPLEMENTED_STATUS = PaperStatus.OUTLINED

# Stages that can be explicitly rebuilt via ``from_stage``.
REBUILD_STAGES = ("metadata", "parse", "abstract", "summary", "outline")

# Abstract cleaning is explicit but non-blocking, so it has a rebuild position without a
# corresponding PaperStatus checkpoint.
_STAGE_INDEX = {"metadata": 1, "parse": 2, "abstract": 3, "summary": 4, "outline": 5}
_STATUS_STAGE_INDEX = {
    PaperStatus.DISCOVERED: 0,
    PaperStatus.METADATA_RESOLVED: 1,
    PaperStatus.PARSED: 2,
    PaperStatus.SUMMARIZED: 4,
    PaperStatus.OUTLINED: 5,
}


class UpdateTargetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateFailure:
    paper_id: str
    message: str
    category: str = ""


@dataclass(frozen=True, slots=True)
class UpdateEvent:
    """Structured progress for one update step; adapters decide how to render it."""

    paper_id: str | None
    stage: str
    current: int
    total: int
    message: str


UpdateEventCallback = Callable[[UpdateEvent], None]


@dataclass(slots=True)
class UpdateResult:
    target_status: PaperStatus
    updated: list[PaperRecord] = field(default_factory=list)
    skipped: list[PaperRecord] = field(default_factory=list)
    warnings: list[UpdateFailure] = field(default_factory=list)
    failures: list[UpdateFailure] = field(default_factory=list)


def rebuild_stage_index(from_stage: str) -> int:
    try:
        return _STAGE_INDEX[from_stage]
    except KeyError:
        raise UpdateTargetError(
            f"Unknown stage: {from_stage}; expected one of {', '.join(REBUILD_STAGES)}"
        ) from None


def status_stage_index(status: PaperStatus) -> int:
    return _STATUS_STAGE_INDEX[status]
