from dataclasses import dataclass, field
from pathlib import Path

from passagen.storage.repository import PaperRecord


class ScanDirectoryError(ValueError):
    pass


class InvalidPdfError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScanFailure:
    path: Path
    message: str
    code: str = "import_failed"


@dataclass(slots=True)
class ScanResult:
    imported: list[PaperRecord] = field(default_factory=list)
    skipped: list[PaperRecord] = field(default_factory=list)
    failures: list[ScanFailure] = field(default_factory=list)
