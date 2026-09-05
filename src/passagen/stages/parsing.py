import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from passagen.config import GrobidSettings, ParserBackend, ParsingSettings
from passagen.domain import PaperStatus
from passagen.parsing import PaperParser, ParsedPaper, ParsingError
from passagen.providers import ProviderHealthSnapshot, ProviderUnavailableError
from passagen.providers.parsing import parse_document
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.storage.files import atomic_write_bytes
from passagen.storage.repository import (
    ArtifactRecord,
    PaperRecord,
    get_artifact,
    get_paper,
    save_parsed_artifact,
    update_paper_status,
)

logger = logging.getLogger(__name__)
EXTRACTED_ARTIFACT_KIND = "extracted_json"


class PaperParsingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PaperParsingResult:
    paper: PaperRecord
    artifact: ArtifactRecord | None
    parsed: ParsedPaper | None
    warnings: tuple[str, ...] = ()
    updated: bool = True


def parse_paper(
    database_path: Path,
    data_dir: Path,
    paper_id: str,
    settings: ParsingSettings,
    grobid_settings: GrobidSettings,
    *,
    provider_health: ProviderHealthSnapshot | None = None,
    parser: ParserBackend | None = None,
    force: bool = False,
    grobid: PaperParser | None = None,
    pymupdf_parser: PaperParser | None = None,
    progress: ProgressCallback | None = None,
) -> PaperParsingResult:
    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise PaperParsingError("paper_not_found", f"Paper not found: {paper_id}")
    existing = get_artifact(database_path, paper_id, EXTRACTED_ARTIFACT_KIND)
    if paper.status is not PaperStatus.METADATA_RESOLVED and not force:
        if paper.status in {
            PaperStatus.PARSED,
            PaperStatus.SUMMARIZED,
            PaperStatus.OUTLINED,
        }:
            return PaperParsingResult(paper=paper, artifact=existing, parsed=None, updated=False)
        raise PaperParsingError(
            "metadata_required",
            f"Paper must have resolved metadata before parsing: {paper_id}",
        )
    if force and paper.status is not PaperStatus.METADATA_RESOLVED:
        update_paper_status(database_path, paper_id, PaperStatus.METADATA_RESOLVED)
    if paper.managed_pdf_path is None:
        raise PaperParsingError("missing_pdf", f"Paper has no managed PDF artifact: {paper_id}")
    pdf_path = data_dir / paper.managed_pdf_path
    if not pdf_path.is_file():
        raise PaperParsingError("missing_pdf", f"Managed PDF does not exist: {pdf_path}")

    backend = parser or settings.parser
    warnings: list[str] = []
    report_progress(progress, f"Parsing full text with {backend.value}...")
    try:
        parsed = parse_document(
            pdf_path,
            backend,
            settings,
            grobid_settings,
            health=provider_health,
            grobid=grobid,
            pymupdf_parser=pymupdf_parser,
            report=lambda message: report_progress(progress, message),
        )
    except ProviderUnavailableError as exc:
        raise PaperParsingError("grobid_unavailable", str(exc)) from exc
    except ParsingError as exc:
        logger.error("parse failed: paper_id=%s code=%s error=%s", paper_id, exc.code, exc)
        raise PaperParsingError(exc.code, str(exc)) from exc

    report_progress(progress, "Writing extracted.json...")
    relative_path = Path("papers") / paper_id / "extracted.json"
    content = (parsed.model_dump_json(indent=2) + "\n").encode()
    atomic_write_bytes(data_dir / relative_path, content, prefix="extracted-")
    digest = hashlib.sha256(content).hexdigest()
    updated, artifact = save_parsed_artifact(
        database_path,
        paper_id,
        relative_path,
        version=parsed.schema_version,
        sha256=digest,
        size_bytes=len(content),
        status=PaperStatus.PARSED,
        abstract=parsed.metadata.abstract,
        abstract_source="pdf" if parsed.parser == "pymupdf" else parsed.parser,
    )
    logger.info(
        "parse finished: paper_id=%s parser=%s sections=%s references=%s artifact=%s",
        paper_id,
        parsed.parser,
        len(parsed.sections),
        len(parsed.references),
        relative_path,
    )
    report_progress(
        progress,
        f"Full text parsed with {parsed.parser}: {len(parsed.sections)} section(s), "
        f"{len(parsed.references)} reference(s).",
    )
    return PaperParsingResult(
        paper=updated,
        artifact=artifact,
        parsed=parsed,
        warnings=tuple(warnings),
    )
