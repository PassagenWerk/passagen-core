from pathlib import Path

import httpx
import pymupdf
import pytest

from passagen.catalog import CatalogService
from passagen.config import GrobidSettings, ParserBackend, ParsingSettings
from passagen.domain import BibliographicMetadata, PaperStatus
from passagen.external.parsing import GrobidFulltextParser
from passagen.parsing import ParsedMetadata, ParsedPaper, ParsedSection, ParsingError, PyMuPdfParser
from passagen.providers import ProviderHealthSnapshot, ProviderStatus
from passagen.stages.abstracts import backfill_abstracts
from passagen.stages.parsing import PaperParsingError, parse_paper
from passagen.stages.scanning import scan_directory
from passagen.storage.repository import get_artifact, update_paper_metadata, update_paper_status


def write_structured_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Structured Paper Title", fontsize=20)
        page.insert_text((72, 110), "Abstract", fontsize=15)
        page.insert_text((72, 140), "An author-written overview of the paper.", fontsize=10)
        page.insert_text((72, 180), "1 Introduction", fontsize=15)
        page.insert_text((72, 210), "Introduction body with enough text for parsing.", fontsize=10)
        page = document.new_page()
        page.insert_text((72, 72), "2 Evaluation", fontsize=15)
        page.insert_text((72, 105), "Evaluation body reports useful results.", fontsize=10)
        document.set_metadata(
            {"title": "Structured Paper Title", "author": "Ada Lovelace; Alan Turing"}
        )
        document.save(path)


def test_pymupdf_parser_extracts_sections_and_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    write_structured_pdf(pdf_path)

    parsed = PyMuPdfParser(min_text_characters=10).parse(pdf_path)

    assert parsed.parser == "pymupdf"
    assert parsed.metadata.title == "Structured Paper Title"
    assert parsed.metadata.abstract == "An author-written overview of the paper."
    assert parsed.metadata.authors == ("Ada Lovelace", "Alan Turing")
    assert any(section.title == "1 Introduction" for section in parsed.sections)
    assert any(section.title == "2 Evaluation" for section in parsed.sections)
    assert {page for section in parsed.sections for page in section.pages} == {1, 2}


def test_pymupdf_parser_rejects_pdf_without_text_layer(tmp_path: Path) -> None:
    pdf_path = tmp_path / "blank.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(pdf_path)

    with pytest.raises(ParsingError, match="no extractable text layer") as error:
        PyMuPdfParser(min_text_characters=10).parse(pdf_path)

    assert error.value.code == "no_text_layer"


def test_grobid_parser_health_and_tei_structure(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    tei = b"""<TEI xmlns="http://www.tei-c.org/ns/1.0">
<teiHeader><fileDesc><titleStmt><title>GROBID Paper</title></titleStmt>
<sourceDesc><biblStruct><analytic>
<author><persName><forename>Ada</forename><surname>Lovelace</surname></persName></author>
<idno type="DOI">10.1000/GROBID</idno></analytic>
<monogr><title>Test Conference</title><imprint><date when="2025"/></imprint></monogr>
</biblStruct></sourceDesc></fileDesc>
<profileDesc><abstract><p>An author-written GROBID overview.</p></abstract></profileDesc>
</teiHeader>
<text><body><div><head coords="1,1,1,1,1">1 Introduction</head>
<p coords="1,1,2,1,1">First paragraph.</p>
<p coords="2,1,1,1,1">Second paragraph.</p></div></body><back><div><listBibl>
<biblStruct><analytic><title level="a">Referenced Work</title>
<author><persName><forename>Alan</forename><surname>Turing</surname></persName></author>
<idno type="DOI">10.1000/REF</idno></analytic>
<monogr><imprint><date when="2020"/></imprint></monogr>
<note type="raw_reference">Alan Turing. Referenced Work. 2020.</note></biblStruct>
</listBibl></div></back></text></TEI>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/isalive":
            return httpx.Response(200, text="true")
        assert request.url.path == "/api/processFulltextDocument"
        assert b'form-data; name="teiCoordinates"' in request.content
        assert request.content.count(b'form-data; name="teiCoordinates"') == 3
        return httpx.Response(200, content=tei)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = GrobidFulltextParser(
            base_url="https://grobid.test",
            timeout_seconds=1,
            client=client,
        )
        parsed = parser.parse(pdf_path)

    assert parsed.parser == "grobid"
    assert parsed.metadata.title == "GROBID Paper"
    assert parsed.metadata.abstract == "An author-written GROBID overview."
    assert parsed.metadata.authors == ("Ada Lovelace",)
    assert parsed.metadata.doi == "10.1000/grobid"
    assert parsed.sections[0].pages == (1, 2)
    assert parsed.references[0].title == "Referenced Work"
    assert parsed.references[0].doi == "10.1000/ref"


def test_parse_service_auto_falls_back_and_saves_artifact(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_structured_pdf(source_path)
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    update_paper_metadata(
        database_path,
        paper.id,
        BibliographicMetadata(title="Structured Paper Title", sources={"title": "pdf"}),
        PaperStatus.METADATA_RESOLVED,
    )

    with pytest.raises(PaperParsingError, match="health check failed") as error:
        parse_paper(
            database_path,
            data_dir,
            paper.id,
            ParsingSettings(parser=ParserBackend.AUTO),
            GrobidSettings(),
            provider_health=ProviderHealthSnapshot(
                {"grobid": ProviderStatus("grobid", False, "health check failed")}
            ),
        )

    assert error.value.code == "grobid_unavailable"


def test_parse_service_persists_abstract_with_parser_source(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_structured_pdf(source_path)
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    update_paper_metadata(
        database_path,
        paper.id,
        BibliographicMetadata(title="Structured Paper Title", sources={"title": "pdf"}),
        PaperStatus.METADATA_RESOLVED,
    )

    result = parse_paper(
        database_path,
        data_dir,
        paper.id,
        ParsingSettings(parser=ParserBackend.PYMUPDF, min_text_characters=10),
        GrobidSettings(),
    )

    assert result.paper.abstract == "An author-written overview of the paper."
    assert result.paper.metadata_sources["abstract"] == "pdf"

    catalog = CatalogService(database_path, data_dir)
    current = catalog.get_paper(paper.id)
    catalog.update_user_metadata(
        paper.id,
        abstract="A corrected author abstract.",
        expected_updated_at=current.updated_at,
    )
    reparsed = parse_paper(
        database_path,
        data_dir,
        paper.id,
        ParsingSettings(parser=ParserBackend.PYMUPDF, min_text_characters=10),
        GrobidSettings(),
        force=True,
    )

    assert reparsed.paper.abstract == "A corrected author abstract."
    assert reparsed.paper.metadata_sources["abstract"] == "user"


def test_abstract_backfill_preserves_status_artifacts_and_user_edits(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "paper.pdf"
    write_structured_pdf(source_path)
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    paper = scan_directory(
        source_path.parent,
        data_dir=data_dir,
        database_path=database_path,
    ).imported[0]
    update_paper_status(database_path, paper.id, PaperStatus.OUTLINED)

    class FakeParser:
        name = "fake"
        calls = 0

        def parse(self, path: Path) -> ParsedPaper:
            assert path.is_file()
            self.calls += 1
            return ParsedPaper(
                metadata=ParsedMetadata(abstract="  A backfilled author abstract.  "),
                sections=(ParsedSection(title="Introduction", text="Body"),),
                parser=self.name,
            )

    parser = FakeParser()
    result = backfill_abstracts(
        database_path,
        data_dir,
        ParsingSettings(parser=ParserBackend.PYMUPDF),
        GrobidSettings(),
        pymupdf_parser=parser,
    )

    assert parser.calls == 1
    assert len(result.updated) == 1
    assert result.updated[0].abstract == "A backfilled author abstract."
    assert result.updated[0].status is PaperStatus.OUTLINED
    assert result.updated[0].metadata_sources["abstract"] == "fake"
    assert get_artifact(database_path, paper.id, "extracted_json") is None

    skipped = backfill_abstracts(
        database_path,
        data_dir,
        ParsingSettings(parser=ParserBackend.PYMUPDF),
        GrobidSettings(),
        pymupdf_parser=parser,
    )
    assert parser.calls == 1
    assert len(skipped.skipped) == 1

    catalog = CatalogService(database_path, data_dir)
    current = catalog.get_paper(paper.id)
    catalog.update_user_metadata(
        paper.id,
        abstract="A user-corrected abstract.",
        expected_updated_at=current.updated_at,
    )
    forced = backfill_abstracts(
        database_path,
        data_dir,
        ParsingSettings(parser=ParserBackend.PYMUPDF),
        GrobidSettings(),
        force=True,
        pymupdf_parser=parser,
    )
    assert parser.calls == 1
    assert forced.skipped[0].abstract == "A user-corrected abstract."
    assert forced.skipped[0].status is PaperStatus.OUTLINED
