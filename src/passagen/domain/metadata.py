from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BibliographicMetadata:
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    source_url: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
