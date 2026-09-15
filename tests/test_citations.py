from pathlib import Path

import pytest

from passagen.catalog import CatalogNotFoundError
from passagen.citations import CitationService, CitationSource
from passagen.domain import BibliographicMetadata, Paper
from passagen.external.citations import CitationLookupError
from passagen.storage.database import initialize_database
from passagen.storage.repository import register_pdf, update_paper_metadata


def _paper(tmp_path: Path, metadata: BibliographicMetadata) -> tuple[Path, str]:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    paper = Paper(original_filename="paper.pdf", pdf_sha256="a" * 64, file_size_bytes=1)
    register_pdf(database_path, paper, Path("pdfs/aa/paper.pdf"))
    update_paper_metadata(database_path, paper.id, metadata, status=paper.status)
    return database_path, paper.id


class _DoiLookup:
    def __init__(self, result: str | None = None, error: str | None = None) -> None:
        self.result = result
        self.error = error

    def lookup(self, doi: str) -> str | None:
        del doi
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
    assert result.content.startswith("@misc{author2025fast,")
    assert "author = {Ada Author and Grace Hopper}" in result.content
    assert "title = {Fast \\& Safe\\_Systems}" in result.content
    assert "doi = {10.1000/fallback}" in result.content
    assert result.warnings and "DOI BibTeX lookup failed" in result.warnings[0]


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
