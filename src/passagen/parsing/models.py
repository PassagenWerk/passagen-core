from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ParsingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ParsedMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None


class ParsedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    text: str
    pages: tuple[int, ...] = ()


class ParsedReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None


class ParsedPaper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    metadata: ParsedMetadata = Field(default_factory=ParsedMetadata)
    sections: tuple[ParsedSection, ...]
    references: tuple[ParsedReference, ...] = ()
    parser: str


class PaperParser(Protocol):
    name: str

    def parse(self, path: Path) -> ParsedPaper: ...
