from pathlib import Path
from typing import Protocol

from passagen.config import ProvidersSettings
from passagen.domain import (
    BibliographicMetadata,
    extract_arxiv_id,
    extract_doi,
    normalize_arxiv_id,
    normalize_doi,
)
from passagen.external.metadata import (
    ArxivClient,
    CrossrefClient,
    GrobidClient,
    MetadataLookup,
    MetadataLookupError,
    PdfMetadataLookup,
)
from passagen.parsing.metadata import PdfMetadataError, extract_pdf_metadata

__all__ = [
    "ConfiguredMetadataProvider",
    "BibliographicMetadata",
    "MetadataLookup",
    "MetadataLookupError",
    "MetadataProvider",
    "PdfMetadataLookup",
    "PdfMetadataError",
    "extract_arxiv_id",
    "extract_doi",
    "extract_pdf_metadata",
    "merge_metadata",
    "normalize_arxiv_id",
    "normalize_doi",
]


class MetadataProvider(Protocol):
    def crossref(self, identifier: str) -> BibliographicMetadata | None: ...

    def arxiv(self, identifier: str) -> BibliographicMetadata | None: ...

    def grobid(self, path: Path) -> BibliographicMetadata | None: ...


def merge_metadata(*items: BibliographicMetadata) -> BibliographicMetadata:
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    source_url: str | None = None
    sources: dict[str, str] = {}
    for item in items:
        for name in ("title", "authors", "year", "venue", "doi", "arxiv_id", "source_url"):
            if getattr(item, name) not in (None, (), "") and (source := item.sources.get(name)):
                sources[name] = source
        title = item.title or title
        authors = item.authors or authors
        year = item.year or year
        venue = item.venue or venue
        doi = item.doi or doi
        arxiv_id = item.arxiv_id or arxiv_id
        source_url = item.source_url or source_url
    return BibliographicMetadata(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        source_url=source_url,
        sources=sources,
    )


class ConfiguredMetadataProvider:
    def __init__(
        self,
        settings: ProvidersSettings,
        *,
        crossref: MetadataLookup | None = None,
        arxiv: MetadataLookup | None = None,
        grobid: PdfMetadataLookup | None = None,
    ) -> None:
        self.crossref_client = crossref or CrossrefClient(
            base_url=settings.crossref.base_url,
            timeout_seconds=settings.crossref.timeout_seconds,
            mailto=settings.crossref.mailto,
        )
        self.arxiv_client = arxiv or ArxivClient(
            base_url=settings.arxiv.base_url,
            timeout_seconds=settings.arxiv.timeout_seconds,
        )
        self.grobid_client = grobid or GrobidClient(
            base_url=settings.grobid.base_url,
            timeout_seconds=settings.grobid.timeout_seconds,
        )

    def crossref(self, identifier: str) -> BibliographicMetadata | None:
        return self.crossref_client.lookup(identifier)

    def arxiv(self, identifier: str) -> BibliographicMetadata | None:
        return self.arxiv_client.lookup(identifier)

    def grobid(self, path: Path) -> BibliographicMetadata | None:
        return self.grobid_client.extract(path)
