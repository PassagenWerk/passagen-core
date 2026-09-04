from collections.abc import Callable
from pathlib import Path

from passagen.config import GrobidSettings, ParserBackend, ParsingSettings
from passagen.external.parsing import GrobidFulltextParser
from passagen.parsing import PaperParser, ParsedPaper, PyMuPdfParser
from passagen.providers.health import ProviderHealthSnapshot


def parse_document(
    path: Path,
    backend: ParserBackend,
    settings: ParsingSettings,
    grobid_settings: GrobidSettings,
    *,
    health: ProviderHealthSnapshot | None = None,
    grobid: PaperParser | None = None,
    pymupdf_parser: PaperParser | None = None,
    report: Callable[[str], None] | None = None,
) -> ParsedPaper:
    if backend is ParserBackend.PYMUPDF:
        parser = pymupdf_parser or PyMuPdfParser(min_text_characters=settings.min_text_characters)
    else:
        if health is not None:
            health.require("grobid")
        if report is not None:
            report("GROBID is available; parsing TEI full text...")
        parser = grobid or GrobidFulltextParser(
            base_url=grobid_settings.base_url,
            timeout_seconds=grobid_settings.timeout_seconds,
        )
    return parser.parse(path)
