from dataclasses import dataclass

from passagen.storage.repository import PaperRecord


class MetadataResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MetadataResolutionResult:
    paper: PaperRecord
    warnings: tuple[str, ...] = ()
    updated: bool = True
