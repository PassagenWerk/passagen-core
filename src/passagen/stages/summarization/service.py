from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from passagen.config import LlmSettings, SummarizationSettings, SummarizationStrategy
from passagen.domain import PaperStatus
from passagen.parsing import ParsedPaper
from passagen.prompting import (
    PromptTemplate,
    PromptTemplateError,
    SummaryPromptTemplates,
    load_summary_prompt_templates,
)
from passagen.providers import (
    LlmCallStats,
    LlmProvider,
    LlmProviderError,
    LlmResponse,
    LlmStage,
    OpenAICompatibleProvider,
    ProviderHealthSnapshot,
    ProviderUnavailableError,
    TokenBudget,
    TrackedLlmProvider,
    retry_truncated_response,
)
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.stages.summarization.chunking import PaperChunk, build_chunks
from passagen.stages.summarization.evidence import group_by_domain, merge_evidence
from passagen.stages.summarization.schema import (
    SUMMARY_SCHEMA_VERSION,
    EvidenceItem,
    ExtractedEvidence,
    StructuredSummary,
    SummaryIdentity,
)
from passagen.storage.files import atomic_write_bytes
from passagen.storage.repository import (
    ArtifactRecord,
    PaperRecord,
    finish_processing_run,
    get_artifact,
    get_paper,
    record_llm_call,
    save_summary_artifacts,
    start_processing_run,
    update_paper_status,
)

logger = logging.getLogger(__name__)
SUMMARY_PROMPT_VERSION = "3"
EXTRACTED_ARTIFACT_KIND = "extracted_json"
SUMMARY_ARTIFACT_KIND = "summary_json"


class SummaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SummaryResult:
    paper: PaperRecord
    artifact: ArtifactRecord | None
    summary: StructuredSummary | None
    updated: bool


def summarize_paper(
    database_path: Path,
    data_dir: Path,
    paper_id: str,
    settings: LlmSettings,
    summarization: SummarizationSettings,
    *,
    provider_health: ProviderHealthSnapshot | None = None,
    force: bool = False,
    provider: LlmProvider | None = None,
    execution_log_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    llm_stats: LlmCallStats | None = None,
) -> SummaryResult:
    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise SummaryError(f"Paper not found: {paper_id}")
    existing = get_artifact(database_path, paper_id, SUMMARY_ARTIFACT_KIND)
    if paper.status in {PaperStatus.SUMMARIZED, PaperStatus.OUTLINED} and not force:
        return SummaryResult(paper, existing, None, updated=False)
    extracted = get_artifact(database_path, paper_id, EXTRACTED_ARTIFACT_KIND)
    if extracted is None:
        raise SummaryError(f"Paper must be parsed before summarization: {paper_id}")
    try:
        parsed = ParsedPaper.model_validate_json(
            (data_dir / extracted.path).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise SummaryError(f"Cannot load parsed paper for summarization: {exc}") from exc
    if force and paper.status is not PaperStatus.PARSED:
        update_paper_status(database_path, paper_id, PaperStatus.PARSED)

    try:
        prompts = load_summary_prompt_templates(
            summarization.facts_prompt_path,
            summarization.summary_prompt_path,
            summarization.repair_prompt_path,
            summarization.full_prompt_path,
            summarization.reduce_prompt_path,
        )
    except PromptTemplateError as exc:
        raise SummaryError(str(exc)) from exc

    if provider_health is not None:
        try:
            provider_health.require("llm")
        except ProviderUnavailableError as exc:
            raise SummaryError(str(exc)) from exc
    llm = TrackedLlmProvider(provider or OpenAICompatibleProvider(settings), llm_stats)
    run_id = start_processing_run(database_path, paper_id, "summarize")
    evidence_dir = data_dir / "papers" / paper_id / "summary" / "evidence"
    call_log_dir = (
        execution_log_dir / "external" / "llm" / paper_id if execution_log_dir is not None else None
    )
    budget = TokenBudget.from_settings(settings)
    try:
        strategy = _resolve_strategy(
            summarization.strategy,
            budget,
            prompts.full,
            paper,
            parsed,
            summarization.summary_max_output_tokens,
        )
        logger.info(
            "summary strategy: paper_id=%s configured=%s selected=%s",
            paper_id,
            summarization.strategy.value,
            strategy.value,
        )
        report_progress(progress, f"Summarizing with the {strategy.value} strategy...")
        if strategy is SummarizationStrategy.FULL:
            summary = _summarize_full(
                parsed,
                paper,
                prompts.full,
                prompts.repair,
                llm,
                database_path,
                run_id,
                call_log_dir,
                summarization.summary_max_output_tokens,
                progress,
            )
        else:
            summary = _summarize_hierarchical(
                parsed,
                paper,
                prompts,
                budget,
                llm,
                database_path,
                run_id,
                call_log_dir,
                evidence_dir,
                summarization,
                progress,
            )
        summary = summary.model_copy(update={"identity": _summary_identity(paper)})
        json_path = Path("papers") / paper_id / "summary.json"
        yaml_path = Path("papers") / paper_id / "summary.yaml"
        json_content = (summary.model_dump_json(indent=2) + "\n").encode()
        yaml_content = yaml.safe_dump(
            summary.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        ).encode()
        atomic_write_bytes(data_dir / json_path, json_content, prefix="summary-")
        atomic_write_bytes(data_dir / yaml_path, yaml_content, prefix="summary-")
        updated, artifact = save_summary_artifacts(
            database_path,
            paper_id,
            json_path,
            yaml_path,
            version=SUMMARY_SCHEMA_VERSION,
            json_sha256=hashlib.sha256(json_content).hexdigest(),
            json_size_bytes=len(json_content),
            yaml_sha256=hashlib.sha256(yaml_content).hexdigest(),
            yaml_size_bytes=len(yaml_content),
        )
    except (LlmProviderError, SummaryError) as exc:
        finish_processing_run(database_path, run_id, error_message=str(exc))
        logger.error("summary failed: paper_id=%s error=%s", paper_id, exc)
        raise SummaryError(str(exc)) from exc
    except KeyboardInterrupt:
        finish_processing_run(database_path, run_id, error_message="interrupted")
        raise
    finish_processing_run(database_path, run_id)
    report_progress(progress, "Structured summary saved.")
    logger.info("summary finished: paper_id=%s artifact=%s", paper_id, json_path)
    return SummaryResult(updated, artifact, summary, updated=True)


def _resolve_strategy(
    configured: SummarizationStrategy,
    budget: TokenBudget,
    full_template: PromptTemplate,
    paper: PaperRecord,
    parsed: ParsedPaper,
    max_output_tokens: int,
) -> SummarizationStrategy:
    if configured is SummarizationStrategy.HIERARCHICAL:
        return SummarizationStrategy.HIERARCHICAL
    prompt = _full_prompt(full_template, paper, parsed)
    estimated = budget.estimate_tokens(prompt)
    available = budget.available_input_tokens(max_output_tokens)
    fits = estimated <= available
    logger.info(
        "summary budget: estimated_full_tokens=%d available_input_tokens=%d fits=%s",
        estimated,
        available,
        fits,
    )
    if configured is SummarizationStrategy.FULL:
        if not fits:
            raise SummaryError(
                "Full-paper summary request exceeds the context budget: estimated "
                f"{estimated} input tokens, available {available}. Use strategy 'auto' or "
                "'hierarchical', or adjust the providers.llm budget settings."
            )
        return SummarizationStrategy.FULL
    return SummarizationStrategy.FULL if fits else SummarizationStrategy.HIERARCHICAL


def _summarize_full(
    parsed: ParsedPaper,
    paper: PaperRecord,
    full_template: PromptTemplate,
    repair_template: PromptTemplate,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    call_log_dir: Path | None,
    max_output_tokens: int,
    progress: ProgressCallback | None,
) -> StructuredSummary:
    report_progress(progress, "Generating structured summary from the full paper...")
    raw_response = _generate(
        provider,
        _full_prompt(full_template, paper, parsed),
        database_path,
        run_id,
        call_log_dir / "summary-full.json" if call_log_dir is not None else None,
        "summary (full)",
        max_output_tokens,
        LlmStage.SUMMARY,
    )
    return _validate_or_repair(
        raw_response.content,
        provider,
        database_path,
        run_id,
        call_log_dir,
        max_output_tokens,
        progress,
        repair_template,
    )


def _summarize_hierarchical(
    parsed: ParsedPaper,
    paper: PaperRecord,
    prompts: SummaryPromptTemplates,
    budget: TokenBudget,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    call_log_dir: Path | None,
    evidence_dir: Path,
    summarization: SummarizationSettings,
    progress: ProgressCallback | None,
) -> StructuredSummary:
    overhead = budget.estimate_tokens(prompts.evidence.render(schema=_evidence_schema(), chunk=""))
    try:
        chunks = build_chunks(
            parsed,
            max_input_tokens=summarization.chunk_max_input_tokens,
            prompt_overhead_tokens=overhead,
            overlap_paragraphs=summarization.chunk_overlap_paragraphs,
            measure=budget.estimate_tokens,
        )
    except ValueError as exc:
        raise SummaryError(str(exc)) from exc
    if not chunks:
        raise SummaryError("Parsed paper has no text sections")
    items: list[EvidenceItem] = []
    for chunk in chunks:
        extracted = _chunk_evidence(
            chunk,
            len(chunks),
            evidence_dir,
            summarization.fact_max_output_tokens,
            call_log_dir,
            provider,
            database_path,
            run_id,
            progress,
            prompts.evidence,
        )
        items.extend(extracted.evidence)
    merged = merge_evidence(items)
    logger.info(
        "evidence merged: chunks=%d extracted=%d merged=%d",
        len(chunks),
        len(items),
        len(merged),
    )
    prompt = _summary_prompt(prompts.summary, paper, merged)
    if not budget.fits(prompt, summarization.summary_max_output_tokens):
        report_progress(progress, "Condensing evidence to fit the summary budget...")
        merged = merge_evidence(
            _condense_evidence(
                merged,
                prompts.reduce,
                budget,
                provider,
                database_path,
                run_id,
                call_log_dir,
                summarization.fact_max_output_tokens,
            )
        )
        prompt = _summary_prompt(prompts.summary, paper, merged)
        if not budget.fits(prompt, summarization.summary_max_output_tokens):
            raise SummaryError(
                "Condensed evidence still exceeds the summary context budget; "
                "increase the providers.llm budget settings"
            )
    report_progress(progress, "Generating structured summary...")
    raw_response = _generate(
        provider,
        prompt,
        database_path,
        run_id,
        call_log_dir / "summary.json" if call_log_dir is not None else None,
        "summary",
        summarization.summary_max_output_tokens,
        LlmStage.SUMMARY,
    )
    return _validate_or_repair(
        raw_response.content,
        provider,
        database_path,
        run_id,
        call_log_dir,
        summarization.summary_max_output_tokens,
        progress,
        prompts.repair,
    )


def _chunk_evidence(
    chunk: PaperChunk,
    total: int,
    evidence_dir: Path,
    max_output_tokens: int,
    call_log_dir: Path | None,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    progress: ProgressCallback | None,
    prompt_template: PromptTemplate,
) -> ExtractedEvidence:
    digest = hashlib.sha256(f"{prompt_template.sha256}\0{chunk.text}".encode()).hexdigest()
    path = evidence_dir / f"chunk-{digest}.json"
    if path.exists():
        try:
            cached = str(json.loads(path.read_text(encoding="utf-8"))["response"])
            report_progress(progress, f"Reusing chunk evidence {chunk.index}/{total}.")
            return ExtractedEvidence.model_validate_json(cached)
        except (OSError, KeyError, TypeError, ValueError, ValidationError):
            pass
    report_progress(progress, f"Extracting chunk evidence {chunk.index}/{total}...")
    response = _generate_evidence(
        provider,
        prompt_template.render(schema=_evidence_schema(), chunk=chunk.text),
        database_path,
        run_id,
        call_log_dir,
        chunk.index,
        total,
        max_output_tokens,
        progress,
    )
    try:
        validated = ExtractedEvidence.model_validate_json(response.content)
    except ValidationError as exc:
        raise SummaryError(f"Extracted evidence failed schema validation: {exc}") from exc
    atomic_write_bytes(
        path,
        json.dumps(
            {
                "prompt_version": SUMMARY_PROMPT_VERSION,
                "prompt_sha256": prompt_template.sha256,
                "response": validated.model_dump_json(),
            }
        ).encode(),
        prefix="summary-",
    )
    return validated


def _generate_evidence(
    provider: TrackedLlmProvider,
    prompt: str,
    database_path: Path,
    run_id: str,
    call_log_dir: Path | None,
    index: int,
    total: int,
    max_output_tokens: int,
    progress: ProgressCallback | None,
    *,
    max_attempts: int = 3,
) -> LlmResponse:
    def request(attempt: int, attempt_tokens: int) -> LlmResponse:
        suffix = "" if attempt == 1 else f"-retry-{attempt}"
        diagnostic = (
            call_log_dir / f"evidence-{index:03d}{suffix}.json"
            if call_log_dir is not None
            else None
        )
        return _generate(
            provider,
            prompt,
            database_path,
            run_id,
            diagnostic,
            f"evidence {index}/{total}{suffix.replace('-', ' ')}",
            attempt_tokens,
            LlmStage.EVIDENCE,
        )

    def is_valid(content: str) -> bool:
        try:
            ExtractedEvidence.model_validate_json(content)
        except ValidationError:
            return False
        return True

    def on_retry(attempt_tokens: int) -> None:
        report_progress(
            progress,
            f"Retrying chunk evidence {index}/{total} with {attempt_tokens} output tokens "
            "(previous response was truncated)...",
        )

    return retry_truncated_response(
        request,
        initial_max_tokens=max_output_tokens,
        is_valid=is_valid,
        max_attempts=max_attempts,
        on_retry=on_retry,
    )


def _condense_evidence(
    items: list[EvidenceItem],
    reduce_template: PromptTemplate,
    budget: TokenBudget,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    call_log_dir: Path | None,
    max_output_tokens: int,
) -> list[EvidenceItem]:
    condensed: list[EvidenceItem] = []
    for domain, group in group_by_domain(items).items():
        condensed.extend(
            _condense_group(
                domain,
                group,
                reduce_template,
                budget,
                provider,
                database_path,
                run_id,
                call_log_dir,
                max_output_tokens,
            )
        )
    return condensed


def _condense_group(
    domain: str,
    group: list[EvidenceItem],
    reduce_template: PromptTemplate,
    budget: TokenBudget,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    call_log_dir: Path | None,
    max_output_tokens: int,
) -> list[EvidenceItem]:
    prompt = reduce_template.render(schema=_evidence_schema(), evidence=_evidence_json(group))
    if budget.fits(prompt, max_output_tokens):
        response = _generate(
            provider,
            prompt,
            database_path,
            run_id,
            call_log_dir / f"reduce-{domain}.json" if call_log_dir is not None else None,
            f"reduce {domain}",
            max_output_tokens,
            LlmStage.SUMMARY,
        )
        try:
            return ExtractedEvidence.model_validate_json(response.content).evidence
        except ValidationError:
            logger.warning("evidence condense for domain %s failed validation; splitting", domain)
    if len(group) == 1:
        logger.warning(
            "single evidence item exceeds the condense budget; keeping it unchanged",
        )
        return group
    middle = len(group) // 2
    return _condense_group(
        domain,
        group[:middle],
        reduce_template,
        budget,
        provider,
        database_path,
        run_id,
        call_log_dir,
        max_output_tokens,
    ) + _condense_group(
        domain,
        group[middle:],
        reduce_template,
        budget,
        provider,
        database_path,
        run_id,
        call_log_dir,
        max_output_tokens,
    )


def _generate(
    provider: TrackedLlmProvider,
    prompt: str,
    database_path: Path,
    run_id: str,
    diagnostic_path: Path | None,
    label: str,
    max_tokens: int,
    purpose: LlmStage,
) -> LlmResponse:
    diagnostic = {
        "label": label,
        "provider": provider.provider_name,
        "model": provider.model,
        "max_tokens": max_tokens,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    if diagnostic_path is not None:
        atomic_write_bytes(
            diagnostic_path,
            json.dumps(diagnostic, ensure_ascii=False, indent=2).encode(),
            prefix="summary-",
        )
    logger.info(
        "llm request: label=%s model=%s prompt_chars=%s max_tokens=%s diagnostic=%s",
        label,
        provider.model,
        len(prompt),
        max_tokens,
        diagnostic_path or "not_saved",
    )
    logger.debug("llm request content: label=%s\n%s", label, prompt)
    try:
        response = provider.generate(purpose, prompt, max_tokens=max_tokens)
    except LlmProviderError as exc:
        record_llm_call(
            database_path,
            run_id,
            provider=provider.provider_name,
            model=provider.model,
            prompt_version=SUMMARY_PROMPT_VERSION,
            schema_version=SUMMARY_SCHEMA_VERSION,
            input_tokens=None,
            output_tokens=None,
            error_message=str(exc),
        )
        diagnostic["error"] = str(exc)
        if diagnostic_path is not None:
            atomic_write_bytes(
                diagnostic_path,
                json.dumps(diagnostic, ensure_ascii=False, indent=2).encode(),
                prefix="summary-",
            )
        raise
    record_llm_call(
        database_path,
        run_id,
        provider=provider.provider_name,
        model=provider.model,
        prompt_version=SUMMARY_PROMPT_VERSION,
        schema_version=SUMMARY_SCHEMA_VERSION,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
    diagnostic.update(
        {
            "response": response.content,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "reasoning_tokens": response.reasoning_tokens,
            "finish_reason": response.finish_reason,
        }
    )
    if diagnostic_path is not None:
        atomic_write_bytes(
            diagnostic_path,
            json.dumps(diagnostic, ensure_ascii=False, indent=2).encode(),
            prefix="summary-",
        )
    logger.info(
        "llm response: label=%s response_chars=%s input_tokens=%s output_tokens=%s "
        "reasoning_tokens=%s finish_reason=%s diagnostic=%s",
        label,
        len(response.content),
        response.input_tokens,
        response.output_tokens,
        response.reasoning_tokens,
        response.finish_reason,
        diagnostic_path or "not_saved",
    )
    logger.debug("llm response content: label=%s\n%s", label, response.content)
    return response


def _validate_or_repair(
    raw: str,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    call_log_dir: Path | None,
    max_output_tokens: int,
    progress: ProgressCallback | None,
    repair_template: PromptTemplate,
) -> StructuredSummary:
    current = raw
    for attempt in range(3):
        try:
            return StructuredSummary.model_validate(_decode_json(current))
        except (json.JSONDecodeError, ValidationError) as exc:
            if attempt == 2:
                raise SummaryError(f"Summary failed schema validation: {exc}") from exc
            report_progress(progress, f"Repairing invalid summary ({attempt + 1}/2)...")
            current = _generate(
                provider,
                _repair_prompt(repair_template, current, str(exc)),
                database_path,
                run_id,
                call_log_dir / f"repair-{attempt + 1}.json" if call_log_dir is not None else None,
                f"repair {attempt + 1}/2",
                max_output_tokens,
                LlmStage.SUMMARY,
            ).content
    raise AssertionError("unreachable")


def _decode_json(raw: str) -> dict[str, Any]:
    content = raw.strip()
    if content.startswith("```") and content.endswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise json.JSONDecodeError("Expected JSON object", content, 0)
    return result


def _serialize_paper(parsed: ParsedPaper) -> str:
    parts = []
    for section in parsed.sections:
        title = (section.title or "Untitled").strip() or "Untitled"
        text = section.text.strip()
        if not text:
            continue
        parts.append(f"## {title}\nPages: {list(section.pages)}\n\n{text}")
    return "\n\n".join(parts)


def _full_prompt(template: PromptTemplate, paper: PaperRecord, parsed: ParsedPaper) -> str:
    return template.render(
        schema=json.dumps(StructuredSummary.model_json_schema(), ensure_ascii=False),
        identity=_identity_json(paper),
        paper=_serialize_paper(parsed),
    )


def _evidence_schema() -> str:
    return json.dumps(ExtractedEvidence.model_json_schema(), ensure_ascii=False)


def _evidence_json(items: list[EvidenceItem]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False)


def _summary_prompt(
    template: PromptTemplate,
    paper: PaperRecord,
    evidence: list[EvidenceItem],
) -> str:
    return template.render(
        schema=json.dumps(StructuredSummary.model_json_schema(), ensure_ascii=False),
        identity=_identity_json(paper),
        evidence=_evidence_json(evidence),
    )


def _identity_json(paper: PaperRecord) -> str:
    identity = {
        "title": paper.title,
        "authors": list(paper.authors),
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
    }
    return json.dumps(identity, ensure_ascii=False)


def _summary_identity(paper: PaperRecord) -> SummaryIdentity:
    return SummaryIdentity(
        title=paper.title or paper.original_filename,
        authors=list(paper.authors),
        year=paper.year,
        venue=paper.venue,
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
    )


def _repair_prompt(template: PromptTemplate, raw: str, error: str) -> str:
    return template.render(
        schema=json.dumps(StructuredSummary.model_json_schema(), ensure_ascii=False),
        validation_error=error,
        candidate=raw,
    )
