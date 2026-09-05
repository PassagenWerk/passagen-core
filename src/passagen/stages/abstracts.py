"""Backfill canonical abstracts without rebuilding generated artifacts."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from passagen.config import GrobidSettings, ParserBackend, ParsingSettings
from passagen.parsing import PaperParser, ParsingError
from passagen.providers import ProviderHealthSnapshot, ProviderUnavailableError
from passagen.providers.parsing import parse_document
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.storage.repository import (
    PaperRecord,
    get_paper,
    list_papers,
    update_paper_abstract,
)

logger = logging.getLogger(__name__)


class AbstractBackfillError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AbstractBackfillFailure:
    paper_id: str
    message: str


@dataclass(slots=True)
class AbstractBackfillResult:
    updated: list[PaperRecord] = field(default_factory=list)
    skipped: list[PaperRecord] = field(default_factory=list)
    missing: list[PaperRecord] = field(default_factory=list)
    failures: list[AbstractBackfillFailure] = field(default_factory=list)


def backfill_abstracts(
    database_path: Path,
    data_dir: Path,
    parsing: ParsingSettings,
    grobid_settings: GrobidSettings,
    *,
    paper_ids: list[str] | None = None,
    parser: ParserBackend | None = None,
    force: bool = False,
    provider_health: ProviderHealthSnapshot | None = None,
    grobid: PaperParser | None = None,
    pymupdf_parser: PaperParser | None = None,
    progress: ProgressCallback | None = None,
) -> AbstractBackfillResult:
    papers = _selected_papers(database_path, paper_ids)
    result = AbstractBackfillResult()
    backend = parser or parsing.parser

    for index, paper in enumerate(papers, start=1):
        prefix = f"Paper {index}/{len(papers)}: {paper.id}"
        if paper.metadata_sources.get("abstract") == "user" or (paper.abstract and not force):
            result.skipped.append(paper)
            report_progress(progress, f"{prefix}: abstract already available; skipping.")
            continue
        if paper.managed_pdf_path is None:
            result.failures.append(AbstractBackfillFailure(paper.id, "Paper has no managed PDF"))
            continue
        pdf_path = data_dir / paper.managed_pdf_path
        if not pdf_path.is_file():
            result.failures.append(
                AbstractBackfillFailure(paper.id, f"Managed PDF does not exist: {pdf_path}")
            )
            continue

        report_progress(progress, f"{prefix}: extracting abstract with {backend.value}.")
        try:
            parsed = parse_document(
                pdf_path,
                backend,
                parsing,
                grobid_settings,
                health=provider_health,
                grobid=grobid,
                pymupdf_parser=pymupdf_parser,
            )
        except (ParsingError, ProviderUnavailableError) as exc:
            logger.warning("abstract backfill failed: paper_id=%s error=%s", paper.id, exc)
            result.failures.append(AbstractBackfillFailure(paper.id, str(exc)))
            continue

        abstract = parsed.metadata.abstract
        if not abstract:
            result.missing.append(paper)
            report_progress(progress, f"{prefix}: parser found no abstract.")
            continue
        source = "pdf" if parsed.parser == "pymupdf" else parsed.parser
        updated, changed = update_paper_abstract(
            database_path,
            paper.id,
            abstract,
            source=source,
            overwrite=force,
        )
        (result.updated if changed else result.skipped).append(updated)
        report_progress(progress, f"{prefix}: abstract saved.")

    return result


def _selected_papers(database_path: Path, paper_ids: list[str] | None) -> list[PaperRecord]:
    if paper_ids is None:
        return list_papers(database_path)
    papers: list[PaperRecord] = []
    for paper_id in paper_ids:
        paper = get_paper(database_path, paper_id)
        if paper is None:
            raise AbstractBackfillError(f"Paper not found: {paper_id}")
        papers.append(paper)
    return papers
