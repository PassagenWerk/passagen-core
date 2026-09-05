from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymupdf
import pytest

from passagen.config import MetadataSettings, ProvidersSettings
from passagen.domain import PaperStatus
from passagen.external.metadata import (
    ArxivClient,
    CrossrefClient,
    GrobidClient,
    MetadataLookupError,
)
from passagen.providers.metadata import (
    BibliographicMetadata,
    extract_arxiv_id,
    extract_doi,
    extract_pdf_metadata,
    merge_metadata,
    normalize_arxiv_id,
    normalize_doi,
)
from passagen.stages.metadata import resolve_paper_metadata
from passagen.stages.scanning import scan_directory


def write_pdf(
    path: Path,
    text: str,
    *,
    title: str = "Local Paper Title",
    author: str = "Local Author",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), text)
        document.set_metadata(
            {
                "title": title,
                "author": author,
                "creationDate": "D:20240101000000Z",
            }
        )
        document.save(path)


def test_extracts_and_normalizes_identifiers_from_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    write_pdf(
        pdf_path,
        "DOI: https://doi.org/10.1145/1234.5678.\narXiv:2401.12345v2",
    )

    metadata = extract_pdf_metadata(pdf_path, first_pages=2)

    assert metadata.title == "Local Paper Title"
    assert metadata.authors == ("Local Author",)
    assert metadata.year == 2024
    assert metadata.doi == "10.1145/1234.5678"
    assert metadata.arxiv_id == "2401.12345"
    assert metadata.sources == {
        "title": "pdf",
        "authors": "pdf",
        "year": "pdf",
        "doi": "pdf",
        "arxiv_id": "pdf",
    }


def test_extracts_layout_metadata_after_publisher_cover_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "Gao et al. - Sirius Composing Network Function Chains.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((120, 420), "This paper is included in the", fontsize=18)
        page.insert_text(
            (80, 450),
            "Proceedings of the 21st USENIX Symposium on",
            fontsize=12,
        )
        page.insert_text(
            (80, 468),
            "Networked Systems Design and Implementation.",
            fontsize=12,
        )
        page.insert_text((80, 150), "Sirius: Composing Network Function Chains", fontsize=21)
        page.insert_text((130, 178), "into P4-Capable Edge Gateways", fontsize=21)
        page.insert_text(
            (80, 210),
            "Jiaqi Gao, Jiamin Cao, Yifan Li, and Ennan Zhai, Alibaba Cloud",
            fontsize=14,
        )
        page.insert_text(
            (80, 240),
            "https://www.usenix.org/conference/nsdi24/presentation/gao-jiaqi",
            fontsize=10,
        )
        document.set_metadata({"creationDate": "D:20240306090523Z"})
        document.save(pdf_path)

    metadata = extract_pdf_metadata(
        pdf_path,
        first_pages=2,
        filename_hint=pdf_path.name,
    )

    assert metadata.title == (
        "Sirius: Composing Network Function Chains into P4-Capable Edge Gateways"
    )
    assert metadata.authors == ("Jiaqi Gao", "Jiamin Cao", "Yifan Li", "Ennan Zhai")
    assert metadata.year == 2024
    assert metadata.venue == (
        "21st USENIX Symposium on Networked Systems Design and Implementation"
    )
    assert metadata.source_url == (
        "https://www.usenix.org/conference/nsdi24/presentation/gao-jiaqi"
    )


def test_layout_title_is_not_displaced_by_author_name_in_filename(tmp_path: Path) -> None:
    pdf_path = tmp_path / "nsdi26-dang.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((80, 150), "Mitigating CPU Frontend for Complex", fontsize=21)
        page.insert_text((130, 178), "Data Plane Applications", fontsize=21)
        page.insert_text(
            (80, 210),
            "Yihan Dang, Xi'an Jiaotong University; Hao Li, Example Labs;",
            fontsize=14,
        )
        page.insert_text(
            (80, 228), "Ze Xia, Jiajun Luan, and Peng Zhang, Example University", fontsize=14
        )
        document.save(pdf_path)

    metadata = extract_pdf_metadata(
        pdf_path,
        first_pages=2,
        filename_hint=pdf_path.name,
    )

    assert metadata.title == "Mitigating CPU Frontend for Complex Data Plane Applications"
    assert metadata.authors == (
        "Yihan Dang",
        "Hao Li",
        "Ze Xia",
        "Jiajun Luan",
        "Peng Zhang",
    )


def test_identifier_normalization() -> None:
    assert normalize_doi("https://doi.org/10.1000/ABC.1.") == "10.1000/abc.1"
    assert extract_doi("Published as doi:10.1000/Test-2)") == "10.1000/test-2"
    assert normalize_arxiv_id("arXiv:hep-th/9901001v3") == "hep-th/9901001"
    assert extract_arxiv_id("See arXiv:hep-th/9901001v3") == "hep-th/9901001"


def test_extract_doi_joins_acm_line_wrap_and_prefers_repeated_article_doi() -> None:
    text = """ACM Reference Format:
https://doi.org/10.1145/3789240.
3829171
https://doi.org/10.1145/3789240.3829171
"""

    assert extract_doi(text) == "10.1145/3789240.3829171"


def test_crossref_client_parses_exact_doi_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/works/10.1000%2Fexample"
        return httpx.Response(
            200,
            json={
                "message": {
                    "DOI": "10.1000/example",
                    "title": ["Crossref Title"],
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                    "published-print": {"date-parts": [[2025, 3]]},
                    "container-title": ["Test Conference"],
                    "URL": "https://doi.org/10.1000/example",
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        metadata = CrossrefClient(
            base_url="https://api.crossref.test",
            timeout_seconds=1,
            client=http_client,
        ).lookup("10.1000/example")

    assert metadata is not None
    assert metadata.title == "Crossref Title"
    assert metadata.authors == ("Ada Lovelace",)
    assert metadata.year == 2025
    assert metadata.venue == "Test Conference"
    assert metadata.sources["title"] == "crossref"


def test_arxiv_client_parses_exact_id_response() -> None:
    atom = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/2401.12345v2</id>
    <published>2024-01-20T00:00:00Z</published>
    <title>  An arXiv\n      Paper  </title>
    <summary>  An author-written\n      arXiv overview.  </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:doi>10.1000/example</arxiv:doi>
    <arxiv:journal_ref>Test Journal 12 (2025)</arxiv:journal_ref>
  </entry>
</feed>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id_list"] == "2401.12345"
        return httpx.Response(200, content=atom)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        metadata = ArxivClient(
            base_url="https://export.arxiv.test",
            timeout_seconds=1,
            client=http_client,
        ).lookup("2401.12345")

    assert metadata is not None
    assert metadata.title == "An arXiv Paper"
    assert metadata.abstract == "An author-written arXiv overview."
    assert metadata.authors == ("Ada Lovelace", "Alan Turing")
    assert metadata.year == 2024
    assert metadata.arxiv_id == "2401.12345"
    assert metadata.doi == "10.1000/example"
    assert metadata.sources["arxiv_id"] == "arxiv"


def test_grobid_client_posts_pdf_and_parses_tei_header(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    write_pdf(pdf_path, "Paper body")
    tei = b"""<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt>
        <title>Structured <hi>Paper</hi> Title</title>
      </titleStmt>
      <sourceDesc>
        <biblStruct>
          <analytic>
            <author><persName><forename>Ada</forename><surname>Lovelace</surname></persName></author>
            <author><persName><forename>Alan</forename><surname>Turing</surname></persName></author>
            <idno type="DOI">10.1000/GROBID.1</idno>
            <idno type="arXiv">2401.12345v2</idno>
          </analytic>
          <monogr>
            <title>Test Conference</title>
            <imprint><date when="2025-03-01" /></imprint>
          </monogr>
        </biblStruct>
      </sourceDesc>
    </fileDesc>
    <profileDesc><abstract>
      <p>An author-written <hi>GROBID</hi> overview.</p>
    </abstract></profileDesc>
  </teiHeader>
</TEI>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/processHeaderDocument"
        assert b'form-data; name="input"; filename="paper.pdf"' in request.content
        assert b'form-data; name="consolidateHeader"' in request.content
        return httpx.Response(200, content=tei)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        metadata = GrobidClient(
            base_url="https://grobid.test",
            timeout_seconds=1,
            client=http_client,
        ).extract(pdf_path)

    assert metadata is not None
    assert metadata.title == "Structured Paper Title"
    assert metadata.abstract == "An author-written GROBID overview."
    assert metadata.authors == ("Ada Lovelace", "Alan Turing")
    assert metadata.year == 2025
    assert metadata.venue == "Test Conference"
    assert metadata.doi == "10.1000/grobid.1"
    assert metadata.arxiv_id == "2401.12345"
    assert metadata.sources["title"] == "grobid"


def test_clients_handle_not_found_and_invalid_responses(tmp_path: Path) -> None:
    def crossref_not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def arxiv_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
        )

    with httpx.Client(transport=httpx.MockTransport(crossref_not_found)) as client:
        result = CrossrefClient(
            base_url="https://api.crossref.test",
            timeout_seconds=1,
            client=client,
        ).lookup("10.1000/missing")
    assert result is None

    def grobid_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    pdf_path = tmp_path / "paper.pdf"
    write_pdf(pdf_path, "Paper body")
    with httpx.Client(transport=httpx.MockTransport(grobid_empty)) as client:
        result = GrobidClient(
            base_url="https://grobid.test",
            timeout_seconds=1,
            client=client,
        ).extract(pdf_path)
    assert result is None

    with httpx.Client(transport=httpx.MockTransport(arxiv_empty)) as client:
        result = ArxivClient(
            base_url="https://export.arxiv.test",
            timeout_seconds=1,
            client=client,
        ).lookup("2401.99999")
    assert result is None


def test_clients_convert_service_and_parse_errors(tmp_path: Path) -> None:
    def service_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def invalid_atom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not XML")

    pdf_path = tmp_path / "paper.pdf"
    write_pdf(pdf_path, "Paper body")

    with (
        httpx.Client(transport=httpx.MockTransport(service_error)) as client,
        pytest.raises(MetadataLookupError, match="Crossref lookup failed"),
    ):
        CrossrefClient(
            base_url="https://api.crossref.test",
            timeout_seconds=1,
            client=client,
        ).lookup("10.1000/error")

    with (
        httpx.Client(transport=httpx.MockTransport(invalid_atom)) as client,
        pytest.raises(MetadataLookupError, match="arXiv lookup failed"),
    ):
        ArxivClient(
            base_url="https://export.arxiv.test",
            timeout_seconds=1,
            client=client,
        ).lookup("2401.99999")

    with (
        httpx.Client(transport=httpx.MockTransport(service_error)) as client,
        pytest.raises(MetadataLookupError, match="GROBID header extraction failed"),
    ):
        GrobidClient(
            base_url="https://grobid.test",
            timeout_seconds=1,
            client=client,
        ).extract(pdf_path)


def test_metadata_merge_uses_later_provider_precedence() -> None:
    local = BibliographicMetadata(
        title="PDF title",
        authors=("PDF Author",),
        arxiv_id="2401.12345",
        sources={"title": "pdf", "authors": "pdf", "arxiv_id": "pdf"},
    )
    grobid = BibliographicMetadata(
        title="GROBID title",
        venue="GROBID Conference",
        sources={"title": "grobid", "venue": "grobid"},
    )
    arxiv = BibliographicMetadata(
        title="arXiv title",
        authors=("arXiv Author",),
        arxiv_id="2401.12345",
        sources={"title": "arxiv", "authors": "arxiv", "arxiv_id": "arxiv"},
    )
    crossref = BibliographicMetadata(
        title="Published title",
        doi="10.1000/example",
        sources={"title": "crossref", "doi": "crossref"},
    )

    merged = merge_metadata(local, grobid, arxiv, crossref)

    assert merged.title == "Published title"
    assert merged.authors == ("arXiv Author",)
    assert merged.doi == "10.1000/example"
    assert merged.venue == "GROBID Conference"
    assert merged.sources["title"] == "crossref"
    assert merged.sources["authors"] == "arxiv"
    assert merged.sources["venue"] == "grobid"


@dataclass(slots=True)
class FakeLookup:
    result: BibliographicMetadata | None = None
    error: str | None = None
    identifiers: list[str] = field(default_factory=list)

    def lookup(self, identifier: str) -> BibliographicMetadata | None:
        self.identifiers.append(identifier)
        if self.error:
            raise MetadataLookupError(self.error)
        return self.result


@dataclass(slots=True)
class FakePdfLookup:
    result: BibliographicMetadata | None = None
    error: str | None = None
    paths: list[Path] = field(default_factory=list)

    def extract(self, path: Path) -> BibliographicMetadata | None:
        self.paths.append(path)
        if self.error:
            raise MetadataLookupError(self.error)
        return self.result


def test_resolve_metadata_uses_grobid_when_local_identity_is_incomplete(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(source_path, "Paper body", title="Published Paper", author="")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    grobid = FakePdfLookup(
        BibliographicMetadata(
            title="Published Paper",
            authors=("Ada Lovelace",),
            year=2025,
            venue="Test Conference",
            doi="10.1000/grobid",
            sources={
                "title": "grobid",
                "authors": "grobid",
                "year": "grobid",
                "venue": "grobid",
                "doi": "grobid",
            },
        )
    )
    crossref = FakeLookup(
        BibliographicMetadata(
            title="Published Paper",
            doi="10.1000/grobid",
            sources={"title": "crossref", "doi": "crossref"},
        )
    )

    result = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=crossref,
        arxiv=FakeLookup(error="must not be called"),
        grobid=grobid,
    )

    assert result.paper.title == "Published Paper"
    assert result.paper.authors == ("Ada Lovelace",)
    assert result.paper.venue == "Test Conference"
    assert result.paper.doi == "10.1000/grobid"
    assert result.paper.metadata_sources["title"] == "crossref"
    assert result.paper.metadata_sources["authors"] == "grobid"
    assert result.paper.metadata_sources["venue"] == "grobid"
    assert crossref.identifiers == ["10.1000/grobid"]
    assert len(grobid.paths) == 1


def test_resolve_metadata_rejects_grobid_publisher_cover_identity(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(
        source_path,
        "Paper body without an identifier",
        title="Sirius: Composing Network Function Chains into P4-Capable Edge Gateways",
        author="Jiaqi Gao; Jiamin Cao; Yifan Li; Mengqi Liu; Ming Tang; Dennis Cai; Ennan Zhai",
    )
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    crossref = FakeLookup(error="must not be called")

    result = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=crossref,
        arxiv=FakeLookup(error="must not be called"),
        grobid=FakePdfLookup(
            BibliographicMetadata(
                title="Open access to the Proceedings of the 21st USENIX Symposium on Networked",
                authors=("Systems Design", "Alibaba Cloud"),
                doi="10.1000/publisher-cover",
                sources={"title": "grobid", "authors": "grobid", "doi": "grobid"},
            )
        ),
    )

    assert result.paper.title == (
        "Sirius: Composing Network Function Chains into P4-Capable Edge Gateways"
    )
    assert result.paper.authors == (
        "Jiaqi Gao",
        "Jiamin Cao",
        "Yifan Li",
        "Mengqi Liu",
        "Ming Tang",
        "Dennis Cai",
        "Ennan Zhai",
    )
    assert result.paper.doi is None
    assert result.paper.metadata_sources["title"] == "pdf"
    assert not crossref.identifiers
    assert result.warnings == ("GROBID title does not match PDF title; ignoring response",)


def test_resolve_metadata_uses_grobid_to_recover_crossref_conflict(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(
        source_path,
        "DOI: 10.1145/3789240",
        title="Local Paper Title",
    )
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    crossref = FakeLookup(
        BibliographicMetadata(
            title="Published Paper",
            doi="10.1145/3789240.3829171",
            sources={"title": "crossref", "doi": "crossref"},
        )
    )
    grobid = FakePdfLookup(
        BibliographicMetadata(
            title="Published Paper",
            authors=("Local Author",),
            doi="10.1145/3789240.3829171",
            sources={"title": "grobid", "authors": "grobid", "doi": "grobid"},
        )
    )

    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    service_logger = logging.getLogger("passagen.stages.metadata.service")
    previous_level = service_logger.level
    service_logger.addHandler(log_handler)
    service_logger.setLevel(logging.INFO)
    progress_events: list[str] = []
    try:
        result = resolve_paper_metadata(
            database_path,
            data_dir,
            paper.id,
            MetadataSettings(),
            ProvidersSettings(),
            crossref=crossref,
            arxiv=FakeLookup(error="must not be called"),
            grobid=grobid,
            progress=progress_events.append,
        )
    finally:
        service_logger.removeHandler(log_handler)
        service_logger.setLevel(previous_level)
        log_handler.close()

    assert result.paper.title == "Published Paper"
    assert result.paper.doi == "10.1145/3789240.3829171"
    assert result.paper.metadata_sources["title"] == "crossref"
    assert crossref.identifiers == ["10.1145/3789240", "10.1145/3789240.3829171"]
    assert len(grobid.paths) == 1
    assert not result.warnings
    log_text = log_output.getvalue()
    assert "metadata route selected: provider=Crossref identifier=10.1145/3789240" in log_text
    assert "trying GROBID fallback" in log_text
    assert "metadata route selected: provider=GROBID" in log_text
    assert "metadata DOI corrected by GROBID" in log_text
    assert "provider=Crossref identifier=10.1145/3789240.3829171" in log_text
    assert "Querying Crossref by DOI: 10.1145/3789240" in progress_events
    assert "Crossref title conflict; trying GROBID fallback." in progress_events
    assert "Uploading PDF to GROBID..." in progress_events
    assert "Querying Crossref by DOI: 10.1145/3789240.3829171" in progress_events
    assert progress_events[-1] == "Metadata saved."


def test_resolve_metadata_continues_when_grobid_fails(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(source_path, "Paper body", title="Local Title")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]

    result = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=FakeLookup(error="must not be called"),
        arxiv=FakeLookup(error="must not be called"),
        grobid=FakePdfLookup(error="GROBID unavailable"),
    )

    assert result.paper.title == "Local Title"
    assert result.paper.metadata_sources["title"] == "pdf"
    assert result.warnings == ("GROBID unavailable",)


def test_resolve_metadata_queries_both_providers_and_persists_sources(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(
        source_path,
        "DOI: 10.1000/example\narXiv:2401.12345v2",
        title="Published Title",
    )
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    arxiv = FakeLookup(
        BibliographicMetadata(
            title="arXiv Title",
            authors=("arXiv Author",),
            arxiv_id="2401.12345",
            sources={"title": "arxiv", "authors": "arxiv", "arxiv_id": "arxiv"},
        )
    )
    crossref = FakeLookup(
        BibliographicMetadata(
            title="Published Title",
            year=2025,
            venue="Conference",
            doi="10.1000/example",
            sources={
                "title": "crossref",
                "year": "crossref",
                "venue": "crossref",
                "doi": "crossref",
            },
        )
    )

    result = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=crossref,
        arxiv=arxiv,
        grobid=FakePdfLookup(),
    )

    assert result.paper.status is PaperStatus.METADATA_RESOLVED
    assert result.paper.title == "Published Title"
    assert result.paper.authors == ("arXiv Author",)
    assert result.paper.year == 2025
    assert result.paper.doi == "10.1000/example"
    assert result.paper.arxiv_id == "2401.12345"
    assert result.paper.metadata_sources["title"] == "crossref"
    assert result.paper.metadata_sources["authors"] == "arxiv"
    assert crossref.identifiers == ["10.1000/example"]
    assert arxiv.identifiers == ["2401.12345"]


def test_resolve_metadata_rejects_crossref_title_mismatch(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(source_path, "DOI: 10.1145/3789240", title="VeriLucid Paper")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]

    result = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=FakeLookup(
            BibliographicMetadata(
                title="Proceedings of the ACM SIGCOMM Conference",
                doi="10.1145/3789240",
                sources={"title": "crossref", "doi": "crossref"},
            )
        ),
        arxiv=FakeLookup(error="must not be called"),
        grobid=FakePdfLookup(),
    )

    assert result.paper.title == "VeriLucid Paper"
    assert result.paper.metadata_sources["title"] == "pdf"
    assert paper.managed_pdf_path is not None
    assert result.warnings == (
        f"GROBID did not extract metadata from {paper.managed_pdf_path.name}",
        "Crossref title does not match PDF title for 10.1145/3789240; ignoring response",
    )


def test_resolve_metadata_continues_when_api_fails(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(source_path, "DOI: 10.1000/example")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]

    result = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=FakeLookup(error="Crossref unavailable"),
        arxiv=FakeLookup(error="must not be called"),
        grobid=FakePdfLookup(),
    )

    assert result.paper.status is PaperStatus.METADATA_RESOLVED
    assert result.paper.title == "Local Paper Title"
    assert result.paper.doi == "10.1000/example"
    assert result.paper.metadata_sources["title"] == "pdf"
    assert result.warnings == ("Crossref unavailable",)


def test_resolve_metadata_without_identifiers_does_not_call_api(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_pdf(source_path, "Paper body without an external identifier")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    crossref = FakeLookup(error="must not be called")
    arxiv = FakeLookup(error="must not be called")

    first = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=crossref,
        arxiv=arxiv,
        grobid=FakePdfLookup(),
    )
    second = resolve_paper_metadata(
        database_path,
        data_dir,
        paper.id,
        MetadataSettings(),
        ProvidersSettings(),
        crossref=crossref,
        arxiv=arxiv,
    )

    assert first.paper.status is PaperStatus.METADATA_RESOLVED
    assert first.paper.doi is None
    assert first.paper.arxiv_id is None
    assert second.updated is False
    assert not crossref.identifiers
    assert not arxiv.identifiers
