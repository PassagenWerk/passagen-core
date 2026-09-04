from collections.abc import Callable
from dataclasses import dataclass, field

from passagen.domain import PaperStatus
from passagen.storage.repository import PaperRecord

LATEST_IMPLEMENTED_STATUS = PaperStatus.OUTLINED

# Stages that can be explicitly rebuilt via ``from_stage``; ``outline`` cannot be
# requested directly because rebuilding the summary already rebuilds the outline.
REBUILD_STAGES = ("metadata", "parse", "summary")

# Stage numbers aligned with PaperStatus progression:
# discovered(0) -> metadata(1) -> parse(2) -> summary(3) -> outline(4).
_STAGE_INDEX = {"metadata": 1, "parse": 2, "summary": 3, "outline": 4}
STATUS_ORDER = (
    PaperStatus.DISCOVERED,
    PaperStatus.METADATA_RESOLVED,
    PaperStatus.PARSED,
    PaperStatus.SUMMARIZED,
    PaperStatus.OUTLINED,
)


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
