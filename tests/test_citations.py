import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import passagen.citations as citations
from passagen.catalog import CatalogNotFoundError
from passagen.citations import CitationService, CitationSource
from passagen.domain import BibliographicMetadata, Paper
from passagen.external.citations import CitationLookupError
from passagen.storage.database import initialize_database
from passagen.storage.repository import get_paper, register_pdf, update_paper_metadata


def _paper(tmp_path: Path, metadata: BibliographicMetadata) -> tuple[Path, str]:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    paper = Paper(original_filename="paper.pdf", pdf_sha256="a" * 64, file_size_bytes=1)
    register_pdf(database_path, paper, Path("pdfs/aa/paper.pdf"))
    update_paper_metadata(database_path, paper.id, metadata, status=paper.status)
    return database_path, paper.id


def _citation_key(content: str) -> str:
    match = re.match(r"@misc\{([^,]+),", content)
    assert match is not None
    return match.group(1)


class _DoiLookup:
    def __init__(self, result: str | None = None, error: str | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def lookup(self, doi: str) -> str | None:
        del doi
        self.calls += 1
        if self.error is not None:
            raise CitationLookupError(self.error)
        return self.result


def test_retrieves_authoritative_bibtex_through_doi_content_negotiation(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(
            title="A Paper",
            authors=("Ada Author",),
            year=2025,
            doi="10.1000/example",
            sources={"doi": "crossref"},
        ),
    )
    result = CitationService(
        database_path,
        doi_lookup=_DoiLookup("@article{author2025, title={A Paper}}\n"),
    ).get_bibtex(paper_id)

    assert result.source is CitationSource.DOI
    assert result.authoritative is True
    assert result.content == "@article{author2025, title={A Paper}}\n"
    assert result.warnings == ()
    assert result.cached is False
    assert result.updated_at is not None
    assert result.remote_checked_at is not None


def test_authoritative_citation_is_read_from_persistent_cache(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(title="A Paper", doi="10.1000/cached"),
    )
    first_lookup = _DoiLookup("@article{cached2025, title={A Paper}}\n")
    first = CitationService(database_path, doi_lookup=first_lookup).get_bibtex(paper_id)
    unavailable_lookup = _DoiLookup(error="offline")

    second = CitationService(database_path, doi_lookup=unavailable_lookup).get_bibtex(paper_id)

    assert first.content == second.content
    assert second.cached is True
    assert unavailable_lookup.calls == 0


def test_doi_failure_falls_back_to_escaped_local_metadata(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(
            title="Fast & Safe_Systems",
            authors=("Ada Author", "Grace Hopper"),
            year=2025,
            venue="Systems Conference",
            doi="10.1000/fallback",
            sources={"title": "pdf", "doi": "pdf"},
        ),
    )
    result = CitationService(
        database_path, doi_lookup=_DoiLookup(error="Server error '503 Service Unavailable'")
    ).get_bibtex(paper_id)

    assert result.source is CitationSource.LOCAL
    assert result.authoritative is False
    assert re.match(r"@misc\{author2025fastsafesystems-[0-9a-f]{8},", result.content)
    assert "author = {Ada Author and Grace Hopper}" in result.content
    assert "title = {Fast \\& Safe\\_Systems}" in result.content
    assert "doi = {10.1000/fallback}" in result.content
    assert result.warnings and "DOI BibTeX lookup failed" in result.warnings[0]


def test_failed_doi_cache_waits_until_retry_is_due(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(title="Fallback", doi="10.1000/retry"),
    )
    current = [datetime(2026, 9, 15, tzinfo=UTC)]
    lookup = _DoiLookup(error="offline")
    service = CitationService(database_path, doi_lookup=lookup, now=lambda: current[0])

    first = service.get_bibtex(paper_id)
    second = service.get_bibtex(paper_id)
    current[0] += timedelta(days=2)
    third = service.get_bibtex(paper_id)

    assert first.cached is False
    assert second.cached is True
    assert third.cached is False
    assert lookup.calls == 2


def test_failed_refresh_preserves_authoritative_content(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(title="A Paper", doi="10.1000/refresh"),
    )
    lookup = _DoiLookup("@article{saved2025, title={Saved}}\n")
    service = CitationService(database_path, doi_lookup=lookup)
    original = service.get_bibtex(paper_id)
    lookup.result = None
    lookup.error = "offline"

    refreshed = service.get_bibtex(paper_id, refresh=True)

    assert refreshed.content == original.content
    assert refreshed.source is CitationSource.DOI
    assert refreshed.authoritative is True
    assert refreshed.cached is True
    assert refreshed.warnings and "offline" in refreshed.warnings[0]


def test_invalid_doi_bibtex_is_not_persisted_as_authoritative(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(title="A Paper", doi="10.1000/invalid"),
    )

    result = CitationService(
        database_path,
        doi_lookup=_DoiLookup("@article{missing-delimiter}"),
    ).get_bibtex(paper_id)

    assert result.source is CitationSource.LOCAL
    assert result.authoritative is False
    assert result.warnings and "valid BibTeX key" in result.warnings[0]


def test_arxiv_metadata_generates_an_authoritative_local_entry(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(
            title="Preprint",
            authors=("Ada Author",),
            year=2026,
            arxiv_id="2601.12345",
            sources={"arxiv_id": "arxiv"},
        ),
    )

    result = CitationService(database_path).get_bibtex(paper_id)

    assert result.source is CitationSource.ARXIV
    assert result.authoritative is True
    assert "eprint = {2601.12345}" in result.content
    assert "archivePrefix = {arXiv}" in result.content
    assert "url = {https://arxiv.org/abs/2601.12345}" in result.content


def test_unknown_paper_is_reported_consistently(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(CatalogNotFoundError, match="Paper not found"):
        CitationService(database_path).get_bibtex("missing")


def test_local_keys_are_stable_and_distinguish_similar_papers(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    paper_ids: list[str] = []
    for index, title in enumerate(("The Theory of Systems", "Theory of Systems Extended")):
        paper = Paper(
            original_filename=f"paper-{index}.pdf",
            pdf_sha256=str(index + 1) * 64,
            file_size_bytes=1,
        )
        register_pdf(database_path, paper, Path(f"pdfs/paper-{index}.pdf"))
        update_paper_metadata(
            database_path,
            paper.id,
            BibliographicMetadata(title=title, authors=("Ada Author",), year=2025),
            status=paper.status,
        )
        paper_ids.append(paper.id)

    service = CitationService(database_path)
    first_key = _citation_key(service.get_bibtex(paper_ids[0]).content)
    second_key = _citation_key(service.get_bibtex(paper_ids[1]).content)

    assert first_key.startswith("author2025theorysystems-")
    assert second_key.startswith("author2025theorysystemsextended-")
    assert first_key != second_key
    assert _citation_key(service.get_bibtex(paper_ids[0]).content) == first_key


def test_metadata_change_rebuilds_local_content_but_preserves_key(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(title="Original Title", authors=("Ada Author",), year=2025),
    )
    service = CitationService(database_path)
    original = service.get_bibtex(paper_id)
    cached = service.get_bibtex(paper_id)
    record = get_paper(database_path, paper_id)
    assert record is not None
    update_paper_metadata(
        database_path,
        paper_id,
        BibliographicMetadata(title="Corrected Title", authors=("Ada Author",), year=2025),
        status=record.status,
    )

    rebuilt = service.get_bibtex(paper_id)

    assert cached.cached is True
    assert rebuilt.cached is False
    assert "title = {Corrected Title}" in rebuilt.content
    assert _citation_key(rebuilt.content) == _citation_key(original.content)


def test_normalized_duplicate_identifiers_are_disambiguated(tmp_path: Path) -> None:
    database_path, first_id = _paper(
        tmp_path,
        BibliographicMetadata(title="Same Work", doi="doi:10.1000/same"),
    )
    second = Paper(original_filename="copy.pdf", pdf_sha256="b" * 64, file_size_bytes=1)
    register_pdf(database_path, second, Path("pdfs/copy.pdf"))
    update_paper_metadata(
        database_path,
        second.id,
        BibliographicMetadata(title="Same Work", doi="10.1000/same"),
        status=second.status,
    )
    service = CitationService(database_path, doi_lookup=_DoiLookup())

    keys = {
        _citation_key(service.get_bibtex(first_id).content),
        _citation_key(service.get_bibtex(second.id).content),
    }

    assert len(keys) == 2


def test_identifier_normalization_keeps_local_key_stable(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(
            title="A Stable Key",
            authors=("Ada Author",),
            year=2025,
            doi="doi:10.1000/EXAMPLE",
        ),
    )
    service = CitationService(database_path, doi_lookup=_DoiLookup())
    original_key = _citation_key(service.get_bibtex(paper_id).content)
    paper = service.get_bibtex(paper_id)
    assert paper.source is CitationSource.LOCAL

    record = get_paper(database_path, paper_id)
    assert record is not None
    stored = update_paper_metadata(
        database_path,
        paper_id,
        BibliographicMetadata(
            title="A Stable Key",
            authors=("Ada Author",),
            year=2025,
            doi="https://doi.org/10.1000/example",
        ),
        status=record.status,
    )
    assert stored.doi == "https://doi.org/10.1000/example"
    assert _citation_key(service.get_bibtex(paper_id).content) == original_key


def test_arxiv_version_normalization_keeps_local_key_stable(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(title="Stable Preprint", arxiv_id="arXiv:2601.12345v2"),
    )
    service = CitationService(database_path)
    original_key = _citation_key(service.get_bibtex(paper_id).content)
    record = get_paper(database_path, paper_id)
    assert record is not None

    update_paper_metadata(
        database_path,
        paper_id,
        BibliographicMetadata(
            title="Stable Preprint",
            arxiv_id="https://arxiv.org/abs/2601.12345",
        ),
        status=record.status,
    )

    assert _citation_key(service.get_bibtex(paper_id).content) == original_key


def test_local_key_has_readable_defaults_without_metadata(tmp_path: Path) -> None:
    database_path, paper_id = _paper(tmp_path, BibliographicMetadata())

    key = _citation_key(CitationService(database_path).get_bibtex(paper_id).content)

    assert re.fullmatch(r"anonymousndpaper-[0-9a-f]{8}", key)


def test_local_key_handles_missing_unicode_and_long_metadata(tmp_path: Path) -> None:
    database_path, paper_id = _paper(
        tmp_path,
        BibliographicMetadata(
            title="深度学习" + " Systems" * 20,
            authors=("Élodie Dùpont",),
        ),
    )

    key = _citation_key(CitationService(database_path).get_bibtex(paper_id).content)

    assert key.startswith("dupontndsystemssystemssystems-")
    assert len(key) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", key)


def test_local_key_expands_digest_when_short_keys_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    paper_ids: list[str] = []
    for index in range(2):
        paper = Paper(
            original_filename=f"paper-{index}.pdf",
            pdf_sha256=str(index + 1) * 64,
            file_size_bytes=1,
        )
        register_pdf(database_path, paper, Path(f"pdfs/paper-{index}.pdf"))
        update_paper_metadata(
            database_path,
            paper.id,
            BibliographicMetadata(title="Same Paper", authors=("Ada Author",), year=2025),
            status=paper.status,
        )
        paper_ids.append(paper.id)

    digests = {
        f"paper:{paper_ids[0]}": "12345678aaaa" + "0" * 52,
        f"paper:{paper_ids[1]}": "12345678bbbb" + "0" * 52,
    }
    monkeypatch.setattr(citations, "_identity_digest", digests.__getitem__)

    keys = [
        _citation_key(CitationService(database_path).get_bibtex(paper_id).content)
        for paper_id in paper_ids
    ]
    assert keys[0].endswith("-12345678aaaa")
    assert keys[1].endswith("-12345678bbbb")
