import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from passagen.catalog import CatalogNotFoundError
from passagen.external.citations import (
    CitationLookupError,
    DoiCitationClient,
    DoiCitationLookup,
)
from passagen.storage.repository import PaperRecord, get_paper


class CitationSource(StrEnum):
    DOI = "doi"
    ARXIV = "arxiv"
    LOCAL = "local_metadata"


@dataclass(frozen=True, slots=True)
class CitationResult:
    paper_id: str
    format: str
    content: str
    source: CitationSource
    authoritative: bool
    warnings: tuple[str, ...] = ()


class CitationService:
    """Retrieve an authoritative DOI citation with a deterministic local fallback."""

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 10.0,
        doi_lookup: DoiCitationLookup | None = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.doi_lookup = doi_lookup or DoiCitationClient(timeout_seconds=timeout_seconds)

    def get_bibtex(self, paper_id: str) -> CitationResult:
        paper = get_paper(self.database_path, paper_id)
        if paper is None:
            raise CatalogNotFoundError(f"Paper not found: {paper_id}")
        warnings: list[str] = []
        if paper.doi is not None:
            try:
                citation = self.doi_lookup.lookup(paper.doi)
            except CitationLookupError as exc:
                warnings.append(f"DOI BibTeX lookup failed; using local metadata: {exc}")
            else:
                if citation is not None:
                    return CitationResult(
                        paper_id=paper.id,
                        format="bibtex",
                        content=citation,
                        source=CitationSource.DOI,
                        authoritative=True,
                    )
                warnings.append("DOI does not provide BibTeX; using local metadata")
        return CitationResult(
            paper_id=paper.id,
            format="bibtex",
            content=_local_bibtex(paper),
            source=CitationSource.ARXIV if paper.arxiv_id is not None else CitationSource.LOCAL,
            authoritative=paper.arxiv_id is not None,
            warnings=tuple(warnings),
        )


def _local_bibtex(paper: PaperRecord) -> str:
    fields: list[tuple[str, str]] = []
    if paper.authors:
        fields.append(("author", " and ".join(paper.authors)))
    if paper.title:
        fields.append(("title", paper.title))
    if paper.year is not None:
        fields.append(("year", str(paper.year)))
    if paper.venue:
        fields.append(("howpublished", paper.venue))
    if paper.doi:
        fields.append(("doi", paper.doi))
    if paper.arxiv_id:
        fields.extend((("eprint", paper.arxiv_id), ("archivePrefix", "arXiv")))
    url = paper.source_url
    if paper.doi:
        url = f"https://doi.org/{paper.doi}"
    elif paper.arxiv_id:
        url = f"https://arxiv.org/abs/{paper.arxiv_id}"
    if url:
        fields.append(("url", url))
    lines = [f"@misc{{{_citation_key(paper)},"]
    lines.extend(f"  {name} = {{{_escape_bibtex(value)}}}," for name, value in fields)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _citation_key(paper: PaperRecord) -> str:
    author = paper.authors[0] if paper.authors else "paper"
    surname = author.split(",", 1)[0] if "," in author else author.rsplit(maxsplit=1)[-1]
    title_words = re.findall(r"[A-Za-z0-9]+", _ascii(paper.title or ""))
    parts = [_key_part(surname) or "paper", str(paper.year or "")]
    if title_words:
        parts.append(title_words[0].lower())
    key = "".join(parts)
    return key or f"paper{paper.id.replace('-', '')[:8]}"


def _key_part(value: str) -> str:
    return "".join(re.findall(r"[A-Za-z0-9]+", _ascii(value))).lower()


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()


def _escape_bibtex(value: str) -> str:
    replacements = {
        "\\": "\\textbackslash{}",
        "{": "\\{",
        "}": "\\}",
        "&": "\\&",
        "%": "\\%",
        "#": "\\#",
        "_": "\\_",
    }
    return "".join(replacements.get(character, character) for character in value)


__all__ = ["CitationResult", "CitationService", "CitationSource"]
