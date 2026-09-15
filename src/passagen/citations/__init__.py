import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from passagen.catalog import CatalogNotFoundError
from passagen.citations.repository import (
    CitationKeyConflictError,
    CitationRecord,
    CitationWrite,
    get_citation,
    list_local_citation_keys,
    upsert_citation,
)
from passagen.domain import normalize_arxiv_id, normalize_doi
from passagen.external.citations import (
    CitationLookupError,
    DoiCitationClient,
    DoiCitationLookup,
)
from passagen.storage.repository import PaperRecord, get_paper, list_papers

_CITATION_KEY_MAX_LENGTH = 64
_GENERATOR_VERSION = "bibtex-v2"
_DIGEST_LENGTHS = (8, 12, 16, 24, 32, 40, 48)
_FAILED_RETRY_DELAY = timedelta(days=1)
_NOT_FOUND_RETRY_DELAY = timedelta(days=7)
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
    cached: bool = False
    updated_at: str | None = None
    remote_checked_at: str | None = None


class CitationService:
    """Retrieve an authoritative DOI citation with a deterministic local fallback."""

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 10.0,
        doi_lookup: DoiCitationLookup | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.doi_lookup = doi_lookup or DoiCitationClient(timeout_seconds=timeout_seconds)
        self.now = now or (lambda: datetime.now(UTC))

    def get_bibtex(self, paper_id: str, *, refresh: bool = False) -> CitationResult:
        return self._get_bibtex(paper_id, refresh=refresh, retry_on_metadata_change=True)

    def _get_bibtex(
        self,
        paper_id: str,
        *,
        refresh: bool,
        retry_on_metadata_change: bool,
    ) -> CitationResult:
        paper = get_paper(self.database_path, paper_id)
        if paper is None:
            raise CatalogNotFoundError(f"Paper not found: {paper_id}")
        fingerprint = _metadata_fingerprint(paper)
        source_identifier = _source_identifier(paper)
        stored = get_citation(self.database_path, paper_id)
        if not refresh and stored is not None:
            if (
                stored.source == CitationSource.DOI
                and stored.source_identifier == source_identifier
            ):
                return _stored_result(stored, cached=True)
            local_is_current = (
                stored.source != CitationSource.DOI
                and stored.metadata_fingerprint == fingerprint
                and stored.generator_version == _GENERATOR_VERSION
            )
            if local_is_current and (
                paper.doi is None or not _retry_due(stored.next_remote_attempt_at, self.now())
            ):
                return _stored_result(stored, cached=True, warnings=_stored_warnings(stored))

        should_try_doi = paper.doi is not None and (
            refresh
            or stored is None
            or stored.source_identifier != source_identifier
            or _retry_due(stored.next_remote_attempt_at, self.now())
        )
        remote_status = stored.remote_status if stored is not None else "not_attempted"
        remote_checked_at = stored.remote_checked_at if stored is not None else None
        next_remote_attempt_at = stored.next_remote_attempt_at if stored is not None else None
        last_error = stored.last_error if stored is not None else None
        warnings: list[str] = []
        if should_try_doi and paper.doi is not None:
            checked_at = self.now()
            try:
                citation = self.doi_lookup.lookup(paper.doi)
            except CitationLookupError as exc:
                remote_status = "failed"
                last_error = f"DOI BibTeX lookup failed: {_safe_error(str(exc))}"
                next_remote_attempt_at = _timestamp(checked_at + _FAILED_RETRY_DELAY)
            else:
                if citation is None:
                    remote_status = "not_found"
                    last_error = "DOI does not provide BibTeX"
                    next_remote_attempt_at = _timestamp(checked_at + _NOT_FOUND_RETRY_DELAY)
                else:
                    try:
                        citation_key = _bibtex_key(citation)
                    except CitationLookupError as exc:
                        remote_status = "failed"
                        last_error = f"DOI BibTeX lookup failed: {_safe_error(str(exc))}"
                        next_remote_attempt_at = _timestamp(checked_at + _FAILED_RETRY_DELAY)
                    else:
                        current = get_paper(self.database_path, paper_id)
                        if current is None:
                            raise CatalogNotFoundError(f"Paper not found: {paper_id}")
                        if (
                            retry_on_metadata_change
                            and _metadata_fingerprint(current) != fingerprint
                        ):
                            return self._get_bibtex(
                                paper_id,
                                refresh=refresh,
                                retry_on_metadata_change=False,
                            )
                        record = upsert_citation(
                            self.database_path,
                            CitationWrite(
                                paper_id=paper.id,
                                content=citation,
                                citation_key=citation_key,
                                source=CitationSource.DOI,
                                authoritative=True,
                                source_identifier=source_identifier,
                                metadata_fingerprint=fingerprint,
                                generator_version=_GENERATOR_VERSION,
                                remote_status="success",
                                remote_checked_at=_timestamp(checked_at),
                            ),
                        )
                        return _stored_result(record, cached=False)
            remote_checked_at = _timestamp(checked_at)
            warnings.append(f"{last_error}; using local metadata")
            if (
                stored is not None
                and stored.source == CitationSource.DOI
                and stored.source_identifier == source_identifier
            ):
                record = upsert_citation(
                    self.database_path,
                    CitationWrite(
                        paper_id=paper.id,
                        content=stored.content,
                        citation_key=stored.citation_key,
                        source=stored.source,
                        authoritative=True,
                        source_identifier=source_identifier,
                        metadata_fingerprint=fingerprint,
                        generator_version=_GENERATOR_VERSION,
                        remote_status=remote_status,
                        remote_checked_at=remote_checked_at,
                        next_remote_attempt_at=next_remote_attempt_at,
                        last_error=last_error,
                    ),
                )
                return _stored_result(record, cached=True, warnings=tuple(warnings))

        current = get_paper(self.database_path, paper_id)
        if current is None:
            raise CatalogNotFoundError(f"Paper not found: {paper_id}")
        if retry_on_metadata_change and _metadata_fingerprint(current) != fingerprint:
            return self._get_bibtex(
                paper_id,
                refresh=refresh,
                retry_on_metadata_change=False,
            )
        preserved_key = (
            stored.citation_key
            if stored is not None and stored.source != CitationSource.DOI
            else None
        )
        content = _local_bibtex(
            paper,
            list_papers(self.database_path),
            citation_key=preserved_key,
            occupied_keys=list_local_citation_keys(self.database_path, exclude_paper_id=paper.id),
        )
        source = CitationSource.ARXIV if paper.arxiv_id is not None else CitationSource.LOCAL
        write = CitationWrite(
            paper_id=paper.id,
            content=content,
            citation_key=_bibtex_key(content),
            source=source,
            authoritative=paper.arxiv_id is not None,
            source_identifier=source_identifier,
            metadata_fingerprint=fingerprint,
            generator_version=_GENERATOR_VERSION,
            remote_status=remote_status if paper.doi is not None else "not_attempted",
            remote_checked_at=remote_checked_at if paper.doi is not None else None,
            next_remote_attempt_at=(next_remote_attempt_at if paper.doi is not None else None),
            last_error=last_error if paper.doi is not None else None,
        )
        try:
            record = upsert_citation(self.database_path, write)
        except CitationKeyConflictError:
            content = _local_bibtex(
                paper,
                list_papers(self.database_path),
                occupied_keys=list_local_citation_keys(
                    self.database_path, exclude_paper_id=paper.id
                ),
            )
            record = upsert_citation(
                self.database_path,
                replace(write, content=content, citation_key=_bibtex_key(content)),
            )
        return _stored_result(record, cached=False, warnings=tuple(warnings))


def _local_bibtex(
    paper: PaperRecord,
    catalog: list[PaperRecord],
    *,
    citation_key: str | None = None,
    occupied_keys: set[str] | None = None,
) -> str:
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
    lines = [f"@misc{{{citation_key or _citation_key(paper, catalog, occupied_keys or set())},"]
    lines.extend(f"  {name} = {{{_escape_bibtex(value)}}}," for name, value in fields)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _citation_key(
    paper: PaperRecord,
    catalog: list[PaperRecord],
    occupied_keys: set[str] | None = None,
) -> str:
    occupied_keys = occupied_keys or set()
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
        if key not in occupied_keys and all(
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


def _metadata_fingerprint(paper: PaperRecord) -> str:
    payload = {
        "arxiv_id": normalize_arxiv_id(paper.arxiv_id) if paper.arxiv_id else None,
        "authors": list(paper.authors),
        "doi": normalize_doi(paper.doi) if paper.doi else None,
        "generator_version": _GENERATOR_VERSION,
        "source_url": paper.source_url,
        "title": paper.title,
        "venue": paper.venue,
        "year": paper.year,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _source_identifier(paper: PaperRecord) -> str | None:
    if paper.doi:
        return f"doi:{normalize_doi(paper.doi)}"
    if paper.arxiv_id:
        return f"arxiv:{normalize_arxiv_id(paper.arxiv_id)}"
    return None


def _bibtex_key(content: str) -> str:
    match = re.match(r"@[A-Za-z]+\s*\{\s*([^,\s]+)\s*,", content)
    if match is None:
        raise CitationLookupError("Citation response does not contain a valid BibTeX key")
    return match.group(1)


def _stored_result(
    record: CitationRecord,
    *,
    cached: bool,
    warnings: tuple[str, ...] = (),
) -> CitationResult:
    return CitationResult(
        paper_id=record.paper_id,
        format=record.format,
        content=record.content,
        source=CitationSource(record.source),
        authoritative=record.authoritative,
        warnings=warnings,
        cached=cached,
        updated_at=record.updated_at,
        remote_checked_at=record.remote_checked_at,
    )


def _stored_warnings(record: CitationRecord) -> tuple[str, ...]:
    if record.last_error is None:
        return ()
    return (f"Using saved local citation after DOI lookup failed: {record.last_error}",)


def _retry_due(value: str | None, now: datetime) -> bool:
    if value is None:
        return True
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= now


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_error(value: str) -> str:
    return " ".join(value.split())[:500] or "DOI lookup failed"


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
