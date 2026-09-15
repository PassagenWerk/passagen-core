import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from passagen.catalog import CatalogNotFoundError
from passagen.domain import normalize_arxiv_id, normalize_doi
from passagen.external.citations import (
    CitationLookupError,
    DoiCitationClient,
    DoiCitationLookup,
)
from passagen.storage.repository import PaperRecord, get_paper, list_papers

_CITATION_KEY_MAX_LENGTH = 64
_DIGEST_LENGTHS = (8, 12, 16, 24, 32, 40, 48)
_TITLE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


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
            content=_local_bibtex(paper, list_papers(self.database_path)),
            source=CitationSource.ARXIV if paper.arxiv_id is not None else CitationSource.LOCAL,
            authoritative=paper.arxiv_id is not None,
            warnings=tuple(warnings),
        )


def _local_bibtex(paper: PaperRecord, catalog: list[PaperRecord]) -> str:
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
    lines = [f"@misc{{{_citation_key(paper, catalog)},"]
    lines.extend(f"  {name} = {{{_escape_bibtex(value)}}}," for name, value in fields)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _citation_key(paper: PaperRecord, catalog: list[PaperRecord]) -> str:
    papers = {candidate.id: candidate for candidate in catalog}
    papers[paper.id] = paper
    identities = {paper_id: _citation_identity(candidate) for paper_id, candidate in papers.items()}
    identity_counts = Counter(identities.values())
    effective_identities = {
        paper_id: identity
        if identity_counts[identity] == 1
        else f"{identity}|paper:{paper_id.lower()}"
        for paper_id, identity in identities.items()
    }
    for digest_length in _DIGEST_LENGTHS:
        key = _format_citation_key(paper, effective_identities[paper.id], digest_length)
        if all(
            candidate_id == paper.id
            or _format_citation_key(
                candidate,
                effective_identities[candidate_id],
                digest_length,
            )
            != key
            for candidate_id, candidate in papers.items()
        ):
            return key
    raise RuntimeError(f"Could not generate a unique citation key for paper {paper.id}")


def _format_citation_key(paper: PaperRecord, identity: str, digest_length: int) -> str:
    author = paper.authors[0] if paper.authors else "anonymous"
    surname = author.split(",", 1)[0] if "," in author else author.rsplit(maxsplit=1)[-1]
    title_words = [
        word.lower()
        for word in re.findall(r"[A-Za-z0-9]+", _ascii(paper.title or ""))
        if word.lower() not in _TITLE_STOP_WORDS
    ][:3]
    title_slug = "".join(title_words) or "paper"
    base = f"{_key_part(surname) or 'anonymous'}{paper.year or 'nd'}{title_slug}"
    digest = _identity_digest(identity)[:digest_length]
    base_limit = _CITATION_KEY_MAX_LENGTH - len(digest) - 1
    return f"{base[:base_limit]}-{digest}"


def _citation_identity(paper: PaperRecord) -> str:
    if paper.doi:
        return f"doi:{normalize_doi(paper.doi)}"
    if paper.arxiv_id:
        return f"arxiv:{normalize_arxiv_id(paper.arxiv_id)}"
    return f"paper:{paper.id.lower()}"


def _identity_digest(identity: str) -> str:
    return sha256(identity.encode("utf-8")).hexdigest()


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
