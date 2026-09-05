from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from passagen.config import PipelineSettings, ProvidersSettings
from passagen.domain import PaperStatus
from passagen.providers import LlmCallStats, LlmProvider, ProviderHealthSnapshot
from passagen.stages.abstract_fixing import AbstractFixError, fix_paper_abstract
from passagen.stages.metadata import MetadataResolutionError, resolve_paper_metadata
from passagen.stages.outlining import OutlineError, outline_paper
from passagen.stages.parsing import PaperParsingError, parse_paper
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.stages.summarization import SummaryError, summarize_paper
from passagen.stages.updating.models import (
    LATEST_IMPLEMENTED_STATUS,
    UpdateEvent,
    UpdateEventCallback,
    UpdateFailure,
    UpdateResult,
    UpdateTargetError,
    rebuild_stage_index,
    status_stage_index,
)
from passagen.storage.repository import PaperRecord, get_paper, list_papers

_UPDATE_PENDING_STATUSES = {
    PaperStatus.DISCOVERED,
    PaperStatus.METADATA_RESOLVED,
    PaperStatus.PARSED,
    PaperStatus.SUMMARIZED,
}
_FAILURE_CATEGORIES: tuple[tuple[type[Exception], str], ...] = (
    (MetadataResolutionError, "metadata"),
    (PaperParsingError, "parsing"),
    (SummaryError, "summarization"),
    (OutlineError, "outlining"),
)
logger = logging.getLogger(__name__)


def update_papers(
    database_path: Path,
    data_dir: Path,
    providers: ProvidersSettings,
    pipeline: PipelineSettings,
    paper_id: str | None = None,
    *,
    paper_ids: Sequence[str] | None = None,
    from_stage: str | None = None,
    summary_provider: LlmProvider | None = None,
    outline_provider: LlmProvider | None = None,
    abstract_provider: LlmProvider | None = None,
    provider_health: ProviderHealthSnapshot | None = None,
    execution_log_dir: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    on_event: UpdateEventCallback | None = None,
    llm_stats: LlmCallStats | None = None,
) -> UpdateResult:
    if from_stage is None and force:
        from_stage = "metadata"
    rebuild_from = rebuild_stage_index(from_stage) if from_stage is not None else None
    papers = _select_papers(database_path, paper_id, paper_ids)
    target_status = LATEST_IMPLEMENTED_STATUS
    rebuild_abstract_only = from_stage == "abstract"
    logger.info(
        "update started: target=%s from_stage=%s selected=%s latest_status=%s",
        paper_id or (f"{len(paper_ids)} ids" if paper_ids is not None else "all"),
        from_stage or "continue",
        len(papers),
        target_status.value,
    )
    _emit(
        on_event,
        progress,
        None,
        "update",
        0,
        len(papers),
        f"Selected {len(papers)} paper(s) for update.",
    )
    result = UpdateResult(target_status=target_status)
    total = len(papers)
    for index, paper in enumerate(papers, start=1):
        if rebuild_from is None and paper.status not in _UPDATE_PENDING_STATUSES:
            logger.info(
                "update skipped: paper_id=%s status=%s reason=already_at_or_beyond_target",
                paper.id,
                paper.status.value,
            )
            result.skipped.append(paper)
            _report_paper_progress(
                progress,
                on_event,
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
        _report_paper_progress(
            progress, on_event, index, total, paper, "selection", "starting update."
        )
        try:
            current = paper
            warnings: list[str] = []
            status_index = status_stage_index(current.status)
            needs_metadata = status_index < 1 or rebuild_from == 1
            needs_parsing = status_index < 2 or (rebuild_from is not None and rebuild_from <= 2)
            needs_abstract_fix = (
                pipeline.abstract_fixing.enabled
                and bool(current.abstract or needs_parsing)
                and (rebuild_from is None or rebuild_from <= 3)
            )
            needs_summary = not rebuild_abstract_only and (
                status_index < 4 or (rebuild_from is not None and rebuild_from <= 4)
            )
            needs_outline = not rebuild_abstract_only and (
                status_index < 5 or rebuild_from is not None
            )
            stage_total = (
                int(needs_metadata)
                + int(needs_parsing)
                + int(needs_abstract_fix)
                + int(needs_summary)
                + int(needs_outline)
            )
            stage_number = 0
            if needs_metadata:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=metadata", paper.id)
                _report_paper_progress(
                    progress,
                    on_event,
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
                    force=rebuild_from == 1,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        on_event,
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
                    on_event,
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
                    force=rebuild_from is not None and rebuild_from <= 2,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        on_event,
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
            if needs_abstract_fix:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=abstract_fix", paper.id)
                _report_paper_progress(
                    progress,
                    on_event,
                    index,
                    total,
                    paper,
                    "abstract clean",
                    "starting.",
                    stage_number=stage_number,
                    stage_total=stage_total,
                )
                try:
                    fixed = fix_paper_abstract(
                        database_path,
                        data_dir,
                        paper.id,
                        providers.llm,
                        pipeline.abstract_fixing,
                        provider_health=provider_health,
                        force=rebuild_from is not None and rebuild_from <= 3,
                        provider=abstract_provider or summary_provider,
                        execution_log_dir=execution_log_dir,
                        progress=partial(
                            _report_paper_progress,
                            progress,
                            on_event,
                            index,
                            total,
                            paper,
                            "abstract clean",
                            stage_number=stage_number,
                            stage_total=stage_total,
                        ),
                        llm_stats=llm_stats,
                    )
                    current = fixed.paper
                except AbstractFixError as exc:
                    warnings.append(str(exc))
                    logger.warning(
                        "update stage warning: paper_id=%s stage=abstract_fix error=%s",
                        paper.id,
                        exc,
                    )
                logger.info("update stage finished: paper_id=%s stage=abstract_fix", paper.id)
            if needs_summary:
                stage_number += 1
                logger.info("update stage started: paper_id=%s stage=summarize", paper.id)
                _report_paper_progress(
                    progress,
                    on_event,
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
                    force=rebuild_from is not None and rebuild_from <= 4,
                    provider=summary_provider,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        on_event,
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
                    on_event,
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
                    force=rebuild_from is not None,
                    provider=outline_provider or summary_provider,
                    progress=partial(
                        _report_paper_progress,
                        progress,
                        on_event,
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
            result.failures.append(
                UpdateFailure(paper.id, str(exc), category=_failure_category(exc))
            )
            _report_paper_progress(
                progress, on_event, index, total, paper, "failed", "update failed; continuing."
            )
            continue
        if needs_metadata or needs_parsing or needs_abstract_fix or needs_summary or needs_outline:
            result.updated.append(current)
            logger.info(
                "update paper finished: paper_id=%s status=%s title=%s",
                paper.id,
                current.status.value,
                current.title,
            )
            _report_paper_progress(progress, on_event, index, total, paper, "complete", "updated.")
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
    _emit(
        on_event,
        progress,
        None,
        "update",
        total,
        total,
        f"Update complete: {len(result.updated)} updated, "
        f"{len(result.skipped)} skipped, {len(result.failures)} failed.",
    )
    return result


def _failure_category(exc: Exception) -> str:
    for error_type, category in _FAILURE_CATEGORIES:
        if isinstance(exc, error_type):
            return category
    return "processing"


def _emit(
    on_event: UpdateEventCallback | None,
    progress: ProgressCallback | None,
    paper_id: str | None,
    stage: str,
    current: int,
    total: int,
    message: str,
) -> None:
    report_progress(progress, message)
    if on_event is not None:
        on_event(UpdateEvent(paper_id, stage, current, total, message))


def _report_paper_progress(
    progress: ProgressCallback | None,
    on_event: UpdateEventCallback | None,
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
    _emit(
        on_event,
        progress,
        paper.id,
        stage,
        index,
        total,
        f"Paper {index}/{total} [{stage_label}]: "
        f"{paper.title or paper.original_filename}: {message}",
    )


def _select_papers(
    database_path: Path,
    paper_id: str | None,
    paper_ids: Sequence[str] | None,
) -> list[PaperRecord]:
    if paper_ids is not None:
        papers = []
        for selected_id in dict.fromkeys(paper_ids):
            paper = get_paper(database_path, selected_id)
            if paper is None:
                raise UpdateTargetError(f"Paper not found: {selected_id}")
            papers.append(paper)
        return papers
    if paper_id is None:
        return list_papers(database_path)
    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise UpdateTargetError(f"Paper not found: {paper_id}")
    return [paper]
