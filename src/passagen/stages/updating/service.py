from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

from passagen.config import PipelineSettings, ProvidersSettings
from passagen.domain import PaperStatus
from passagen.providers import LlmCallStats, LlmProvider, ProviderHealthSnapshot
from passagen.stages.metadata import MetadataResolutionError, resolve_paper_metadata
from passagen.stages.outlining import OutlineError, outline_paper
from passagen.stages.parsing import PaperParsingError, parse_paper
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.stages.summarization import SummaryError, summarize_paper
from passagen.stages.updating.models import (
    LATEST_IMPLEMENTED_STATUS,
    UpdateFailure,
    UpdateResult,
    UpdateTargetError,
)
from passagen.storage.repository import PaperRecord, get_paper, list_papers

_UPDATE_PENDING_STATUSES = {
    PaperStatus.DISCOVERED,
    PaperStatus.METADATA_RESOLVED,
    PaperStatus.PARSED,
    PaperStatus.SUMMARIZED,
}
logger = logging.getLogger(__name__)


def update_papers(
    database_path: Path,
    data_dir: Path,
    providers: ProvidersSettings,
    pipeline: PipelineSettings,
    paper_id: str | None = None,
    *,
    summary_provider: LlmProvider | None = None,
    outline_provider: LlmProvider | None = None,
    provider_health: ProviderHealthSnapshot | None = None,
    execution_log_dir: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    llm_stats: LlmCallStats | None = None,
) -> UpdateResult:
    papers = _select_papers(database_path, paper_id)
    target_status = LATEST_IMPLEMENTED_STATUS
    logger.info(
        "update started: target=%s force=%s selected=%s latest_status=%s",
        paper_id or "all",
        force,
        len(papers),
        target_status.value,
    )
    report_progress(progress, f"Selected {len(papers)} paper(s) for update.")
    result = UpdateResult(target_status=target_status)
    total = len(papers)
    for index, paper in enumerate(papers, start=1):
        if not force and paper.status not in _UPDATE_PENDING_STATUSES:
            logger.info(
                "update skipped: paper_id=%s status=%s reason=already_at_or_beyond_target",
                paper.id,
                paper.status.value,
            )
            result.skipped.append(paper)
            _report_paper_progress(
                progress,
                index,
                total,
                paper,
                "selection",
                "already at or beyond the target status; skipping.",
            )
            continue
        logger.info(
            "update paper started: paper_id=%s status=%s filename=%s",
            paper.id,
            paper.status.value,
            paper.original_filename,
        )
        _report_paper_progress(progress, index, total, paper, "selection", "starting update.")
        try:
            current = paper
            warnings: list[str] = []
            needs_metadata = force or current.status is PaperStatus.DISCOVERED
            needs_parsing = (
                force or needs_metadata or current.status is PaperStatus.METADATA_RESOLVED
            )
            needs_summary = force or needs_parsing or current.status is PaperStatus.PARSED
            needs_outline = force or needs_summary or current.status is PaperStatus.SUMMARIZED
            stage_total = (
                int(needs_metadata) + int(needs_parsing) + int(needs_summary) + int(needs_outline)
            )
            stage_number = 0
            if needs_metadata:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=metadata", paper.id)
                _report_paper_progress(
                    progress,
                    index,
                    total,
                    paper,
                    "metadata",
                    "starting.",
                    stage_number=stage_number,
                    stage_total=stage_total,
                )
                resolution = resolve_paper_metadata(
                    database_path,
                    data_dir,
                    paper.id,
                    pipeline.metadata,
                    providers,
                    provider_health=provider_health,
                    force=force,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        index,
                        total,
                        paper,
                        "metadata",
                        stage_number=1,
                        stage_total=stage_total,
                    ),
                )
                current = resolution.paper
                warnings.extend(resolution.warnings)
                logger.info("update stage finished: paper_id=%s stage=metadata", paper.id)
            if needs_parsing:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=full_text", paper.id)
                _report_paper_progress(
                    progress,
                    index,
                    total,
                    paper,
                    "full text",
                    "starting.",
                    stage_number=stage_number,
                    stage_total=stage_total,
                )
                parsing = parse_paper(
                    database_path,
                    data_dir,
                    paper.id,
                    pipeline.parsing,
                    providers.grobid,
                    provider_health=provider_health,
                    force=force,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        index,
                        total,
                        paper,
                        "full text",
                        stage_number=stage_number,
                        stage_total=stage_total,
                    ),
                )
                current = parsing.paper
                warnings.extend(parsing.warnings)
                logger.info("update stage finished: paper_id=%s stage=full_text", paper.id)
            if needs_summary:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=summarize", paper.id)
                _report_paper_progress(
                    progress,
                    index,
                    total,
                    paper,
                    "summary",
                    "starting.",
                    stage_number=stage_number,
                    stage_total=stage_total,
                )
                summary = summarize_paper(
                    database_path,
                    data_dir,
                    paper.id,
                    providers.llm,
                    pipeline.summarization,
                    provider_health=provider_health,
                    execution_log_dir=execution_log_dir,
                    force=force,
                    provider=summary_provider,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        index,
                        total,
                        paper,
                        "summary",
                        stage_number=stage_number,
                        stage_total=stage_total,
                    ),
                    llm_stats=llm_stats,
                )
                current = summary.paper
                logger.info("update stage finished: paper_id=%s stage=summarize", paper.id)
            if needs_outline:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=outline", paper.id)
                _report_paper_progress(
                    progress,
                    index,
                    total,
                    paper,
                    "outline",
                    "starting.",
                    stage_number=stage_number,
                    stage_total=stage_total,
                )
                outlined = outline_paper(
                    database_path,
                    data_dir,
                    paper.id,
                    providers.llm,
                    pipeline.outlining,
                    provider_health=provider_health,
                    execution_log_dir=execution_log_dir,
                    force=force,
                    provider=outline_provider or summary_provider,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        index,
                        total,
                        paper,
                        "outline",
                        stage_number=stage_number,
                        stage_total=stage_total,
                    ),
                    llm_stats=llm_stats,
                )
                current = outlined.paper
                logger.info("update stage finished: paper_id=%s stage=outline", paper.id)
        except (MetadataResolutionError, PaperParsingError, SummaryError, OutlineError) as exc:
            logger.error("update paper failed: paper_id=%s error=%s", paper.id, exc)
            result.failures.append(UpdateFailure(paper.id, str(exc)))
            _report_paper_progress(
                progress, index, total, paper, "failed", "update failed; continuing."
            )
            continue
        if needs_metadata or needs_parsing or needs_summary or needs_outline:
            result.updated.append(current)
            logger.info(
                "update paper finished: paper_id=%s status=%s title=%s",
                paper.id,
                current.status.value,
                current.title,
            )
            _report_paper_progress(progress, index, total, paper, "complete", "updated.")
        else:
            result.skipped.append(current)
            logger.info("update paper skipped by stage: paper_id=%s", paper.id)
        result.warnings.extend(UpdateFailure(paper.id, warning) for warning in warnings)
        for warning in warnings:
            logger.warning("update paper warning: paper_id=%s warning=%s", paper.id, warning)
    logger.info(
        "update finished: updated=%s skipped=%s warnings=%s failed=%s",
        len(result.updated),
        len(result.skipped),
        len(result.warnings),
        len(result.failures),
    )
    report_progress(
        progress,
        f"Update complete: {len(result.updated)} updated, "
        f"{len(result.skipped)} skipped, {len(result.failures)} failed.",
    )
    return result


def _report_paper_progress(
    progress: ProgressCallback | None,
    index: int,
    total: int,
    paper: PaperRecord,
    stage: str,
    message: str,
    *,
    stage_number: int | None = None,
    stage_total: int | None = None,
) -> None:
    stage_label = (
        f"stage {stage_number}/{stage_total}: {stage}"
        if stage_number is not None and stage_total is not None
        else stage
    )
    report_progress(
        progress,
        f"Paper {index}/{total} [{stage_label}]: "
        f"{paper.title or paper.original_filename}: {message}",
    )


def _select_papers(database_path: Path, paper_id: str | None) -> list[PaperRecord]:
    if paper_id is None:
        return list_papers(database_path)
    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise UpdateTargetError(f"Paper not found: {paper_id}")
    return [paper]
