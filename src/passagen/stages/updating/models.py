from dataclasses import dataclass, field

from passagen.domain import PaperStatus
from passagen.storage.repository import PaperRecord

LATEST_IMPLEMENTED_STATUS = PaperStatus.OUTLINED


class UpdateTargetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateFailure:
    paper_id: str
    message: str


@dataclass(slots=True)
class UpdateResult:
    target_status: PaperStatus
    updated: list[PaperRecord] = field(default_factory=list)
    skipped: list[PaperRecord] = field(default_factory=list)
    warnings: list[UpdateFailure] = field(default_factory=list)
    failures: list[UpdateFailure] = field(default_factory=list)
