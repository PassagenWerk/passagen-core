from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from passagen.config import LlmSettings, OutliningSettings
from passagen.domain import PaperStatus
from passagen.prompting import (
    PromptTemplate,
    PromptTemplateError,
    load_outline_prompt_template,
)
from passagen.providers import (
    LlmCallStats,
    LlmProvider,
    LlmProviderError,
    LlmStage,
    OpenAICompatibleProvider,
    ProviderHealthSnapshot,
    ProviderUnavailableError,
    TrackedLlmProvider,
)
from passagen.stages.outlining.schema import (
    _SECTIONS,
    OUTLINE_SCHEMA_VERSION,
    OutlineSection,
    PaperOutline,
)
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.stages.summarization import SUMMARY_SCHEMA_VERSION, StructuredSummary
from passagen.storage.files import atomic_write_bytes
from passagen.storage.repository import (
    ArtifactRecord,
    PaperRecord,
    finish_processing_run,
    get_artifact,
    get_paper,
    record_llm_call,
    save_outline_artifacts,
    start_processing_run,
    update_paper_status,
)

logger = logging.getLogger(__name__)
OUTLINE_PROMPT_VERSION = "2"
SUMMARY_ARTIFACT_KIND = "summary_json"
OUTLINE_ARTIFACT_KIND = "outline_md"


class OutlineError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OutlineResult:
    paper: PaperRecord
    artifact: ArtifactRecord | None
    outline: PaperOutline | None
    updated: bool


def outline_paper(
    database_path: Path,
    data_dir: Path,
    paper_id: str,
    settings: LlmSettings,
    outlining: OutliningSettings,
    *,
    provider_health: ProviderHealthSnapshot | None = None,
    force: bool = False,
    provider: LlmProvider | None = None,
    execution_log_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    llm_stats: LlmCallStats | None = None,
) -> OutlineResult:
    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise OutlineError(f"Paper not found: {paper_id}")
    existing = get_artifact(database_path, paper_id, OUTLINE_ARTIFACT_KIND)
    if paper.status is PaperStatus.OUTLINED and not force:
        return OutlineResult(paper, existing, None, updated=False)
    summary_artifact = get_artifact(database_path, paper_id, SUMMARY_ARTIFACT_KIND)
    if summary_artifact is None:
        raise OutlineError(f"Paper must have a validated summary before outlining: {paper_id}")
    try:
        summary_content = (data_dir / summary_artifact.path).read_bytes()
        summary = StructuredSummary.model_validate_json(summary_content)
    except (OSError, ValidationError) as exc:
        raise OutlineError(f"Cannot load validated summary for outlining: {exc}") from exc
    if summary.schema_version != SUMMARY_SCHEMA_VERSION:
        raise OutlineError(
            f"Unsupported summary schema version for outlining: {summary.schema_version}"
        )
    if force and paper.status is not PaperStatus.SUMMARIZED:
        update_paper_status(database_path, paper_id, PaperStatus.SUMMARIZED)

    try:
        prompt_template = load_outline_prompt_template(outlining.prompt_path)
    except PromptTemplateError as exc:
        raise OutlineError(str(exc)) from exc

    if provider_health is not None:
        try:
            provider_health.require("llm")
        except ProviderUnavailableError as exc:
            raise OutlineError(str(exc)) from exc
    try:
        llm = TrackedLlmProvider(provider or OpenAICompatibleProvider(settings), llm_stats)
    except LlmProviderError as exc:
        raise OutlineError(str(exc)) from exc
    run_id = start_processing_run(database_path, paper_id, "outline")
    prompt = _outline_prompt(prompt_template, summary)
    diagnostic_path = (
        execution_log_dir / "external" / "llm" / paper_id / "outline.json"
        if execution_log_dir is not None
        else None
    )
    report_progress(progress, "Generating English outline...")
    raw_response: str | None = None
    try:
        response = llm.generate(LlmStage.OUTLINE, prompt, max_tokens=outlining.max_output_tokens)
        raw_response = response.content
        record_llm_call(
            database_path,
            run_id,
            provider=llm.provider_name,
            model=llm.model,
            prompt_version=OUTLINE_PROMPT_VERSION,
            schema_version=OUTLINE_SCHEMA_VERSION,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        _write_diagnostic(
            diagnostic_path,
            prompt,
            llm,
            outlining.max_output_tokens,
            response.content,
        )
        outline = PaperOutline.model_validate(_decode_json(response.content))
        markdown_content = _render_markdown(summary.identity.title, outline).encode()
        summary_sha256 = hashlib.sha256(summary_content).hexdigest()
        source = {
            "outline_schema_version": OUTLINE_SCHEMA_VERSION,
            "prompt_version": OUTLINE_PROMPT_VERSION,
            "prompt_sha256": prompt_template.sha256,
            "summary_schema_version": summary.schema_version,
            "summary_sha256": summary_sha256,
            "provider": llm.provider_name,
            "model": llm.model,
            "prompt": prompt,
            "summary": summary.model_dump(mode="json"),
        }
        source_content = (json.dumps(source, ensure_ascii=False, indent=2) + "\n").encode()
        markdown_path = Path("papers") / paper_id / "outline.md"
        source_path = Path("papers") / paper_id / "outline.source.json"
        atomic_write_bytes(data_dir / markdown_path, markdown_content, prefix="outline-")
        atomic_write_bytes(data_dir / source_path, source_content, prefix="outline-")
        updated, artifact = save_outline_artifacts(
            database_path,
            paper_id,
            markdown_path,
            source_path,
            version=OUTLINE_SCHEMA_VERSION,
            markdown_sha256=hashlib.sha256(markdown_content).hexdigest(),
            markdown_size_bytes=len(markdown_content),
            source_sha256=hashlib.sha256(source_content).hexdigest(),
            source_size_bytes=len(source_content),
        )
    except (LlmProviderError, ValidationError, json.JSONDecodeError, OutlineError) as exc:
        if isinstance(exc, LlmProviderError):
            record_llm_call(
                database_path,
                run_id,
                provider=llm.provider_name,
                model=llm.model,
                prompt_version=OUTLINE_PROMPT_VERSION,
                schema_version=OUTLINE_SCHEMA_VERSION,
                input_tokens=None,
                output_tokens=None,
                error_message=str(exc),
            )
        finish_processing_run(database_path, run_id, error_message=str(exc))
        _write_diagnostic(
            diagnostic_path,
            prompt,
            llm,
            outlining.max_output_tokens,
            response=raw_response,
            error=str(exc),
        )
        logger.error("outline failed: paper_id=%s error=%s", paper_id, exc)
        raise OutlineError(f"English outline generation failed: {exc}") from exc
    except KeyboardInterrupt:
        finish_processing_run(database_path, run_id, error_message="interrupted")
        raise
    finish_processing_run(database_path, run_id)
    report_progress(progress, "English outline saved.")
    logger.info("outline finished: paper_id=%s artifact=%s", paper_id, markdown_path)
    return OutlineResult(updated, artifact, outline, updated=True)


def _outline_prompt(template: PromptTemplate, summary: StructuredSummary) -> str:
    return template.render(
        schema=json.dumps(PaperOutline.model_json_schema(), ensure_ascii=False),
        summary=summary.model_dump_json(),
    )


def _decode_json(raw: str) -> dict[str, Any]:
    content = raw.strip()
    if content.startswith("```") and content.endswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise json.JSONDecodeError("Expected JSON object", content, 0)
    return result


def _render_markdown(title: str, outline: PaperOutline) -> str:
    lines = [f"# {title}: Technical Outline"]
    for field, heading in _SECTIONS:
        section: OutlineSection = getattr(outline, field)
        if not section.has_content:
            continue
        lines.extend(("", f"## {heading}"))
        if section.thesis is not None:
            lines.extend(("", section.thesis))
        for point in section.points:
            lines.extend(("", f"### {point.topic}"))
            lines.extend(f"- {detail}" for detail in point.details)
            if point.evidence_pages:
                pages = ", ".join(str(page) for page in point.evidence_pages)
                lines.append(f"- Evidence pages: {pages}")
    return "\n".join(lines) + "\n"


def _write_diagnostic(
    path: Path | None,
    prompt: str,
    provider: TrackedLlmProvider,
    max_tokens: int,
    response: str | None = None,
    error: str | None = None,
) -> None:
    if path is None:
        return
    document = {
        "label": "outline",
        "provider": provider.provider_name,
        "model": provider.model,
        "max_tokens": max_tokens,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    if response is not None:
        document["response"] = response
    if error is not None:
        document["error"] = error
    atomic_write_bytes(
        path,
        (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(),
        prefix="outline-",
    )
