from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from passagen.config import LlmSettings, SummarizationSettings
from passagen.domain import PaperStatus
from passagen.parsing import ParsedPaper, ParsedSection
from passagen.prompting import (
    PromptTemplate,
    PromptTemplateError,
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
    TrackedLlmProvider,
    retry_truncated_response,
)
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.stages.summarization.schema import (
    SUMMARY_SCHEMA_VERSION,
    ExtractedFacts,
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
SUMMARY_PROMPT_VERSION = "2"
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
    facts_dir = data_dir / "papers" / paper_id / "summary" / "facts"
    call_log_dir = (
        execution_log_dir / "external" / "llm" / paper_id if execution_log_dir is not None else None
    )
    try:
        facts = _section_facts(
            parsed,
            facts_dir,
            summarization.max_chunk_characters,
            summarization.fact_max_output_tokens,
            call_log_dir,
            llm,
            database_path,
            run_id,
            progress,
            prompts.facts,
        )
        report_progress(progress, "Generating structured summary...")
        raw_response = _generate(
            llm,
            _summary_prompt(prompts.summary, paper, facts),
            database_path,
            run_id,
            call_log_dir / "summary.json" if call_log_dir is not None else None,
            "summary",
            summarization.summary_max_output_tokens,
            LlmStage.SUMMARY,
        )
        summary = _validate_or_repair(
            raw_response.content,
            llm,
            database_path,
            run_id,
            call_log_dir,
            summarization.summary_max_output_tokens,
            progress,
            prompts.repair,
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


def _section_facts(
    parsed: ParsedPaper,
    facts_dir: Path,
    max_chunk_characters: int,
    max_output_tokens: int,
    call_log_dir: Path | None,
    provider: TrackedLlmProvider,
    database_path: Path,
    run_id: str,
    progress: ProgressCallback | None,
    prompt_template: PromptTemplate,
) -> list[str]:
    chunks = _chunks(parsed.sections, max_chunk_characters)
    facts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        digest = hashlib.sha256(f"{prompt_template.sha256}\0{chunk}".encode()).hexdigest()
        path = facts_dir / f"section-{digest}.json"
        if path.exists():
            try:
                cached = str(json.loads(path.read_text(encoding="utf-8"))["response"])
                facts.append(ExtractedFacts.model_validate_json(cached).model_dump_json())
                report_progress(progress, f"Reusing section facts {index}/{len(chunks)}.")
                continue
            except (OSError, KeyError, TypeError, ValueError, ValidationError):
                pass
        report_progress(progress, f"Summarizing section facts {index}/{len(chunks)}...")
        response = _generate_facts(
            provider,
            _facts_prompt(prompt_template, chunk),
            database_path,
            run_id,
            call_log_dir,
            index,
            len(chunks),
            max_output_tokens,
            progress,
        )
        try:
            validated = ExtractedFacts.model_validate_json(response.content).model_dump_json()
        except ValidationError as exc:
            raise SummaryError(f"Extracted facts failed schema validation: {exc}") from exc
        atomic_write_bytes(
            path,
            json.dumps(
                {
                    "prompt_version": SUMMARY_PROMPT_VERSION,
                    "prompt_sha256": prompt_template.sha256,
                    "response": validated,
                }
            ).encode(),
            prefix="summary-",
        )
        facts.append(validated)
    return facts


def _generate_facts(
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
        return _generate(
            provider,
            prompt,
            database_path,
            run_id,
            call_log_dir / f"facts-{index:03d}{suffix}.json" if call_log_dir is not None else None,
            f"facts {index}/{total}{suffix.replace('-', ' ')}",
            attempt_tokens,
            LlmStage.FACT,
        )

    def is_valid(content: str) -> bool:
        try:
            ExtractedFacts.model_validate_json(content)
        except ValidationError:
            return False
        return True

    def on_retry(attempt_tokens: int) -> None:
        report_progress(
            progress,
            f"Retrying section facts {index}/{total} with {attempt_tokens} output tokens "
            "(previous response was truncated)...",
        )

    return retry_truncated_response(
        request,
        initial_max_tokens=max_output_tokens,
        is_valid=is_valid,
        max_attempts=max_attempts,
        on_retry=on_retry,
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


def _chunks(sections: tuple[ParsedSection, ...], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for section in sections:
        text = (
            f"Section: {section.title or 'Untitled'}\n"
            f"Pages: {list(section.pages)}\n{section.text}\n"
        )
        while text:
            available = limit - len(current)
            if available <= 0:
                chunks.append(current)
                current = ""
                available = limit
            current += text[:available]
            text = text[available:]
    if current:
        chunks.append(current)
    if not chunks:
        raise SummaryError("Parsed paper has no text sections")
    return chunks


def _facts_prompt(template: PromptTemplate, chunk: str) -> str:
    return template.render(
        schema=json.dumps(ExtractedFacts.model_json_schema(), ensure_ascii=False),
        chunk=chunk,
    )


def _summary_prompt(
    template: PromptTemplate,
    paper: PaperRecord,
    facts: list[str],
) -> str:
    identity = {
        "title": paper.title,
        "authors": list(paper.authors),
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
    }
    return template.render(
        schema=json.dumps(StructuredSummary.model_json_schema(), ensure_ascii=False),
        identity=json.dumps(identity, ensure_ascii=False),
        facts=json.dumps([json.loads(fact) for fact in facts], ensure_ascii=False),
    )


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
