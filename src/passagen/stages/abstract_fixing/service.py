from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from passagen.config import AbstractFixingSettings, LlmSettings
from passagen.prompting import PromptTemplateError, load_abstract_fix_prompt_template
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
from passagen.stages.abstract_fixing.schema import (
    ABSTRACT_FIX_SCHEMA_VERSION,
    AbstractFixCandidate,
    CleanedAbstractArtifact,
)
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.storage.files import atomic_write_bytes
from passagen.storage.repository import (
    ArtifactRecord,
    PaperRecord,
    finish_processing_run,
    get_artifact,
    get_paper,
    record_llm_call,
    save_abstract_fix_artifact,
    start_processing_run,
)

logger = logging.getLogger(__name__)
ABSTRACT_FIX_ARTIFACT_KIND = "abstract_cleaned_json"
ABSTRACT_FIX_PROMPT_VERSION = "1"
_NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)*")


class AbstractFixError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AbstractFixResult:
    paper: PaperRecord
    artifact: ArtifactRecord | None
    cleaned: CleanedAbstractArtifact | None
    updated: bool


def fix_paper_abstract(
    database_path: Path,
    data_dir: Path,
    paper_id: str,
    llm_settings: LlmSettings,
    settings: AbstractFixingSettings,
    *,
    provider_health: ProviderHealthSnapshot | None = None,
    force: bool = False,
    provider: LlmProvider | None = None,
    execution_log_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    llm_stats: LlmCallStats | None = None,
) -> AbstractFixResult:
    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise AbstractFixError(f"Paper not found: {paper_id}")
    if not settings.enabled or not paper.abstract:
        return AbstractFixResult(paper, None, None, updated=False)
    try:
        template = load_abstract_fix_prompt_template(settings.prompt_path)
    except PromptTemplateError as exc:
        raise AbstractFixError(str(exc)) from exc

    raw_hash = hashlib.sha256(paper.abstract.encode()).hexdigest()
    existing = get_artifact(database_path, paper_id, ABSTRACT_FIX_ARTIFACT_KIND)
    expected_model = provider.model if provider is not None else llm_settings.model
    if not force and existing is not None:
        cached = _load_artifact(data_dir, existing)
        if (
            cached is not None
            and cached.raw_abstract_sha256 == raw_hash
            and cached.prompt_sha256 == template.sha256
            and cached.model == expected_model
        ):
            return AbstractFixResult(paper, existing, cached, updated=False)
    if provider_health is not None:
        try:
            provider_health.require("llm")
        except ProviderUnavailableError as exc:
            raise AbstractFixError(str(exc)) from exc

    try:
        selected_provider = provider or OpenAICompatibleProvider(llm_settings)
    except LlmProviderError as exc:
        raise AbstractFixError(str(exc)) from exc
    llm = TrackedLlmProvider(selected_provider, llm_stats)
    run_id = start_processing_run(database_path, paper_id, "abstract_fix")
    prompt = template.render(
        schema=json.dumps(AbstractFixCandidate.model_json_schema(), ensure_ascii=False),
        title=paper.title or paper.original_filename,
        abstract=paper.abstract,
    )
    report_progress(progress, "Cleaning author abstract with the LLM...")
    try:
        response = llm.generate(LlmStage.ABSTRACT, prompt, max_tokens=settings.max_output_tokens)
    except LlmProviderError as exc:
        record_llm_call(
            database_path,
            run_id,
            provider=llm.provider_name,
            model=llm.model,
            prompt_version=ABSTRACT_FIX_PROMPT_VERSION,
            schema_version=ABSTRACT_FIX_SCHEMA_VERSION,
            input_tokens=None,
            output_tokens=None,
            error_message=str(exc),
        )
        finish_processing_run(database_path, run_id, error_message=str(exc))
        raise AbstractFixError(f"Abstract cleaning failed: {exc}") from exc
    record_llm_call(
        database_path,
        run_id,
        provider=llm.provider_name,
        model=llm.model,
        prompt_version=ABSTRACT_FIX_PROMPT_VERSION,
        schema_version=ABSTRACT_FIX_SCHEMA_VERSION,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
    try:
        candidate = AbstractFixCandidate.model_validate(_decode_json(response.content))
        cleaned_text = " ".join(candidate.cleaned_abstract.split())
        _validate_cleaned_abstract(paper.abstract, cleaned_text)
        cleaned = CleanedAbstractArtifact(
            paper_id=paper.id,
            raw_abstract_sha256=raw_hash,
            prompt_sha256=template.sha256,
            model=llm.model,
            cleaned_abstract=cleaned_text,
            corrections=candidate.corrections,
        )
        relative_path = Path("papers") / paper.id / "abstract.cleaned.json"
        content = (cleaned.model_dump_json(indent=2) + "\n").encode()
        atomic_write_bytes(data_dir / relative_path, content, prefix="abstract-")
        artifact = save_abstract_fix_artifact(
            database_path,
            paper.id,
            relative_path,
            version=ABSTRACT_FIX_SCHEMA_VERSION,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
    except (OSError, ValidationError, json.JSONDecodeError, AbstractFixError) as exc:
        finish_processing_run(database_path, run_id, error_message=str(exc))
        raise AbstractFixError(f"Abstract cleaning failed: {exc}") from exc
    finish_processing_run(database_path, run_id)
    report_progress(progress, "Cleaned author abstract saved.")
    return AbstractFixResult(paper, artifact, cleaned, updated=True)


def load_cleaned_abstract(
    database_path: Path,
    data_dir: Path,
    paper_id: str,
) -> CleanedAbstractArtifact | None:
    paper = get_paper(database_path, paper_id)
    if paper is None or not paper.abstract:
        return None
    artifact = get_artifact(database_path, paper_id, ABSTRACT_FIX_ARTIFACT_KIND)
    if artifact is None:
        return None
    cleaned = _load_artifact(data_dir, artifact)
    if cleaned is None:
        return None
    raw_hash = hashlib.sha256(paper.abstract.encode()).hexdigest()
    return cleaned if cleaned.raw_abstract_sha256 == raw_hash else None


def _load_artifact(data_dir: Path, artifact: ArtifactRecord) -> CleanedAbstractArtifact | None:
    try:
        root = data_dir.resolve()
        relative = Path(artifact.path)
        if relative.is_absolute():
            return None
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            return None
        content = path.read_bytes()
        if artifact.size_bytes is not None and len(content) != artifact.size_bytes:
            return None
        if artifact.sha256 is not None and hashlib.sha256(content).hexdigest() != artifact.sha256:
            return None
        return CleanedAbstractArtifact.model_validate_json(content)
    except (OSError, ValidationError):
        return None


def _decode_json(raw: str) -> dict[str, Any]:
    content = raw.strip()
    if content.startswith("```") and content.endswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise json.JSONDecodeError("Expected JSON object", content, 0)
    return result


def _validate_cleaned_abstract(raw: str, cleaned: str) -> None:
    if len(cleaned) < max(40, len(raw) // 4):
        raise AbstractFixError("cleaned abstract removed too much source text")
    if len(cleaned) > len(raw) * 1.15:
        raise AbstractFixError("cleaned abstract expanded beyond the allowed limit")
    raw_numbers = _NUMBER_PATTERN.findall(raw)
    cleaned_numbers = _NUMBER_PATTERN.findall(cleaned)
    if raw_numbers[: len(cleaned_numbers)] != cleaned_numbers:
        raise AbstractFixError("cleaned abstract added or changed a numeric value")
    similarity = SequenceMatcher(None, raw.lower(), cleaned.lower()).ratio()
    if similarity < 0.6:
        raise AbstractFixError("cleaned abstract differs too much from the source text")
