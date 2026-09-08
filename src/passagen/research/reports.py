"""Collection research reports: review, comparison, gaps, and custom.

Reports are source-constrained, citation-checked artifacts. They reuse a
fingerprint-matching collection synthesis when one exists, always persist
their own source snapshot and generation run, and follow the same partial
coverage and stale semantics as collection synthesis.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from passagen.assistant import repository as run_repository
from passagen.assistant.errors import (
    AnswerValidationError,
    AssistantError,
    AssistantNotFoundError,
    CitationValidationError,
    ContextPlanError,
    ProviderCallError,
    ScopeError,
    StaleSourceError,
)
from passagen.assistant.schemas import (
    GenerationStage,
    ReusePolicy,
    SourceSnapshot,
    SourceStatus,
    source_fingerprint,
)
from passagen.assistant.snapshots import build_collection_snapshot
from passagen.assistant.versions import (
    COLLECTION_SYNTHESIS_PROMPT_VERSION,
    COLLECTION_SYNTHESIS_SCHEMA_VERSION,
    REPORT_PROMPT_VERSION,
    REPORT_SCHEMA_VERSION,
)
from passagen.config import AssistantSettings, LlmPurpose, LlmSettings
from passagen.external.llm import LlmProvider, LlmProviderError, LlmResponse
from passagen.prompting.templates import load_prompt_template, load_report_prompt_template
from passagen.providers.budget import TokenBudget
from passagen.providers.llm import resolve_llm_provider
from passagen.research import repository
from passagen.research.renderers import render_report_json, render_report_markdown
from passagen.research.schemas import (
    CollectionArtifact,
    CollectionReport,
    CollectionReportRecord,
    CollectionReportResult,
    CollectionReportView,
    CollectionSynthesis,
    ReportKind,
    SynthesisCoverage,
)
from passagen.research.synthesis import (
    collection_snapshot_changes,
    load_collection_summaries,
    read_synthesis_content,
    validate_summary_citations,
)
from passagen.stages.summarization.schema import StructuredSummary
from passagen.storage.files import atomic_write_bytes

_KIND_TITLES = {
    ReportKind.REVIEW: "Literature review",
    ReportKind.COMPARISON: "Comparison",
    ReportKind.GAPS: "Research gaps",
    ReportKind.CUSTOM: "Research report",
}

_KIND_INSTRUCTIONS = {
    ReportKind.REVIEW: (
        "Write a literature review across the collection: research questions, common themes, "
        "methods, and how the papers relate to each other."
    ),
    ReportKind.COMPARISON: (
        "Compare the papers across methods, data, experimental designs, and results; call out "
        "agreements, contradictions, and complementary contributions."
    ),
    ReportKind.GAPS: (
        "Identify limitations, contradictions, open questions, and research gaps across the "
        "collection, and propose concrete follow-up directions."
    ),
    ReportKind.CUSTOM: ("Complete the following research task using only the supplied sources: "),
}


@dataclass(frozen=True, slots=True)
class ReportSubmission:
    """Outcome of submitting a report: a reusable result or a queued run."""

    report_id: str | None
    run_id: str | None
    result: CollectionReportResult | None


class CollectionReportService:
    """Generate, persist, and inspect citation-checked collection reports."""

    def __init__(
        self,
        database_path: Path,
        data_dir: Path,
        settings: LlmSettings,
        assistant_settings: AssistantSettings | None = None,
        *,
        provider: LlmProvider | None = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.data_dir = data_dir.expanduser().resolve()
        self.settings = settings
        self.assistant_settings = assistant_settings or AssistantSettings()
        self.provider = provider

    def create_report(
        self,
        collection_id: str,
        kind: ReportKind,
        *,
        user_prompt: str | None = None,
        allow_partial: bool = False,
        force: bool = False,
    ) -> CollectionReportResult:
        submission = self.submit_report(
            collection_id,
            kind,
            user_prompt=user_prompt,
            allow_partial=allow_partial,
            force=force,
        )
        if submission.result is not None:
            return submission.result
        if submission.run_id is None:
            raise ScopeError("Collection report submission produced no run")
        return self.execute_report(submission.run_id)

    def submit_report(
        self,
        collection_id: str,
        kind: ReportKind,
        *,
        user_prompt: str | None = None,
        allow_partial: bool = False,
        force: bool = False,
    ) -> ReportSubmission:
        """Reuse a fingerprint-matching report or queue a report generation run."""

        clean_prompt = user_prompt.strip() if user_prompt is not None else None
        if kind is ReportKind.CUSTOM:
            if not clean_prompt:
                raise ScopeError("Custom reports require a non-blank user_prompt")
        elif clean_prompt:
            raise ScopeError("user_prompt is only valid for custom reports")
        snapshot = self._snapshot(collection_id)
        fingerprint = source_fingerprint(snapshot)
        summaries, missing = load_collection_summaries(self.database_path, self.data_dir, snapshot)
        if missing and not allow_partial:
            raise ScopeError(
                "Collection reports require a valid summary_json for every paper; missing or "
                f"invalid: {', '.join(missing)}. Use allow_partial=True to proceed explicitly."
            )
        if not summaries:
            raise ScopeError("Collection has no valid paper summaries for a report")
        title = _report_title(kind, snapshot, clean_prompt)
        if not force:
            record = repository.find_reusable_report(
                self.database_path,
                collection_id=collection_id,
                kind=kind,
                user_prompt=clean_prompt,
                source_fingerprint=fingerprint,
            )
            if record is not None:
                artifacts = repository.report_artifacts(self.database_path, record)
                report = self._read_report(artifacts)
                return ReportSubmission(
                    report_id=record.id,
                    run_id=None,
                    result=CollectionReportResult(
                        record=record,
                        report=report,
                        artifacts=list(artifacts),
                        source_status=SourceStatus(),
                        disposition="reused",
                    ),
                )
        report_id, run_id = repository.create_report_submission(
            self.database_path,
            collection_id=collection_id,
            kind=kind,
            title=title,
            user_prompt=clean_prompt,
            source_snapshot_json=snapshot.model_dump_json(),
            source_fingerprint=fingerprint,
            reuse_policy=(ReusePolicy.FORCE_REGENERATE.value if force else ReusePolicy.AUTO.value),
        )
        return ReportSubmission(report_id=report_id, run_id=run_id, result=None)

    def execute_report(self, run_id: str) -> CollectionReportResult:
        """Execute a queued or claimed report run; used directly and by the worker."""

        run = run_repository.start_generation_run(self.database_path, run_id)
        if run.kind != "report":
            raise ScopeError(f"Generation run {run_id} is not a report run")
        record = repository.get_report_by_run(self.database_path, run_id)
        if record is None:
            raise AssistantNotFoundError(f"No collection report for run {run_id}")
        repository.start_report(self.database_path, record.id)
        try:
            snapshot = SourceSnapshot.model_validate_json(record.source_snapshot_json)
            current = self._snapshot(record.collection_id)
            if source_fingerprint(current) != record.source_fingerprint:
                raise StaleSourceError(
                    f"Collection {record.collection_id} sources changed after this report "
                    "was submitted"
                )
            summaries, missing = load_collection_summaries(
                self.database_path, self.data_dir, snapshot
            )
            if not summaries:
                raise ScopeError("Collection has no valid paper summaries for a report")
            coverage = SynthesisCoverage(
                included_paper_ids=[paper_id for paper_id, _summary, _source in summaries],
                missing_summary_paper_ids=missing,
                partial=bool(missing),
            )
            synthesis, synthesis_artifact_id = self._reusable_synthesis(
                record.collection_id, snapshot
            )
            instructions = _instructions(record.kind, record.user_prompt)
            report = self._generate(run_id, record, instructions, summaries, coverage, synthesis)
            report = report.model_copy(
                update={
                    "kind": record.kind,
                    "title": record.title,
                    "user_prompt": record.user_prompt,
                    "synthesis_artifact_id": synthesis_artifact_id,
                    "coverage": coverage,
                }
            )
            artifacts = self._persist(
                record, run_id, snapshot, report, instructions, summaries, synthesis is not None
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, AssistantError) else "internal_error"
            repository.fail_report(
                self.database_path, record.id, error_code=code, error_message=str(exc)
            )
            raise
        completed = repository.get_report(self.database_path, record.id)
        if completed is None:
            raise AssistantNotFoundError(f"Collection report not found: {record.id}")
        return CollectionReportResult(
            record=completed,
            report=report,
            artifacts=list(artifacts),
            source_status=SourceStatus(),
            disposition="generated",
        )

    def get_report(self, report_id: str) -> CollectionReportView:
        record = repository.get_report(self.database_path, report_id)
        if record is None:
            raise AssistantNotFoundError(f"Collection report not found: {report_id}")
        artifacts = repository.report_artifacts(self.database_path, record)
        report = (
            self._read_report(artifacts) if record.status == "completed" and artifacts else None
        )
        return CollectionReportView(
            record=record,
            report=report,
            artifacts=list(artifacts),
            source_status=self.source_status(record),
        )

    def list_reports(self, collection_id: str) -> tuple[CollectionReportView, ...]:
        return tuple(
            CollectionReportView(
                record=record,
                report=None,
                artifacts=[],
                source_status=self.source_status(record),
            )
            for record in repository.list_reports(self.database_path, collection_id)
        )

    def source_status(self, record: CollectionReportRecord) -> SourceStatus:
        if record.status != "completed":
            return SourceStatus()
        try:
            saved = SourceSnapshot.model_validate_json(record.source_snapshot_json)
            current = self._snapshot(record.collection_id)
        except (ValidationError, ScopeError):
            return SourceStatus(stale=True, reasons=["source_missing"])
        reasons = collection_snapshot_changes(saved, current)
        return SourceStatus(stale=bool(reasons), reasons=reasons)

    def _snapshot(self, collection_id: str) -> SourceSnapshot:
        snapshot = build_collection_snapshot(self.database_path, self.data_dir, collection_id)
        return snapshot.model_copy(
            update={
                "prompt_version": REPORT_PROMPT_VERSION,
                "answer_schema_version": REPORT_SCHEMA_VERSION,
            }
        )

    def _reusable_synthesis(
        self, collection_id: str, snapshot: SourceSnapshot
    ) -> tuple[CollectionSynthesis | None, str | None]:
        """Load the fingerprint-matching synthesis, if one was generated."""

        synthesis_fingerprint = source_fingerprint(
            snapshot.model_copy(
                update={
                    "prompt_version": COLLECTION_SYNTHESIS_PROMPT_VERSION,
                    "answer_schema_version": COLLECTION_SYNTHESIS_SCHEMA_VERSION,
                }
            )
        )
        artifacts = repository.latest_synthesis_artifacts(
            self.database_path, collection_id, source_fingerprint=synthesis_fingerprint
        )
        if not artifacts:
            return None, None
        synthesis = read_synthesis_content(self.data_dir, artifacts)
        artifact_id = next((item.id for item in artifacts if item.kind == "synthesis_json"), None)
        return synthesis, artifact_id

    def _generate(
        self,
        run_id: str,
        record: CollectionReportRecord,
        instructions: str,
        summaries: list[tuple[str, StructuredSummary, dict[str, object]]],
        coverage: SynthesisCoverage,
        synthesis: CollectionSynthesis | None,
    ) -> CollectionReport:
        sources = _sources_payload(summaries, synthesis)
        prompt = load_report_prompt_template(self.assistant_settings.report_prompt_path).render(
            schema=_report_schema(),
            instructions=instructions,
            sources=sources,
        )
        max_tokens = self.assistant_settings.report_max_output_tokens
        response = self._call_llm(
            run_id, GenerationStage.ANSWER, LlmPurpose.REPORT_ANSWER, prompt, max_tokens=max_tokens
        )
        try:
            return self._parse_and_validate(response.content, summaries, coverage)
        except (
            ValidationError,
            CitationValidationError,
            AnswerValidationError,
            ScopeError,
        ) as exc:
            repair = load_prompt_template(
                "qa-repair-v3.txt",
                None,
                variables={"schema", "question", "context", "validation_error", "candidate"},
            ).render(
                schema=_report_schema(),
                question=instructions,
                context=sources,
                validation_error=str(exc),
                candidate=response.content,
            )
            repaired = self._call_llm(
                run_id,
                GenerationStage.REPAIR,
                LlmPurpose.REPORT_REPAIR,
                repair,
                max_tokens=max_tokens,
            )
            try:
                return self._parse_and_validate(repaired.content, summaries, coverage)
            except (ValidationError, CitationValidationError, ScopeError) as repair_error:
                raise AnswerValidationError(
                    f"Collection report failed validation after one repair: {repair_error}"
                ) from repair_error

    def _parse_and_validate(
        self,
        content: str,
        summaries: list[tuple[str, StructuredSummary, dict[str, object]]],
        coverage: SynthesisCoverage,
    ) -> CollectionReport:
        try:
            report = CollectionReport.model_validate_json(content)
        except ValidationError as exc:
            detail = exc.json(include_input=False)
            raise AnswerValidationError(f"Report failed schema validation: {detail}") from exc
        cited = {citation.paper_id for citation in report.citations}
        outside = cited - set(coverage.included_paper_ids)
        if outside:
            raise ScopeError("Report cites papers outside coverage: " + ", ".join(sorted(outside)))
        validate_summary_citations(report.citations, summaries)
        return report

    def _fits(self, prompt: str, purpose: LlmPurpose, output_tokens: int) -> bool:
        _name, profile = self.settings.resolve(purpose)
        budget = TokenBudget.from_settings(profile)
        input_limit = min(
            self.assistant_settings.collection_max_input_tokens,
            budget.available_input_tokens(output_tokens),
        )
        return budget.estimate_tokens(prompt) <= input_limit

    def _call_llm(
        self,
        run_id: str,
        stage: GenerationStage,
        purpose: LlmPurpose,
        prompt: str,
        *,
        max_tokens: int,
    ) -> LlmResponse:
        if not self._fits(prompt, purpose, max_tokens):
            raise ContextPlanError(f"{stage.value} prompt exceeds collection token limits")
        resolved = resolve_llm_provider(self.settings, purpose, self.provider)
        provider = resolved.provider
        call_id = str(uuid.uuid4())
        diagnostic_dir = self.data_dir / "runs" / run_id / "llm" / call_id
        self._write_diagnostic(
            diagnostic_dir / "request.json",
            {
                "stage": stage.value,
                "purpose": purpose.value,
                "profile": resolved.profile_name,
                "provider": provider.provider_name,
                "model": provider.model,
                "max_tokens": max_tokens,
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            },
        )
        try:
            response = provider.generate(prompt, max_tokens=max_tokens)
        except LlmProviderError as exc:
            run_repository.record_generation_llm_call(
                self.database_path,
                run_id,
                call_id=call_id,
                stage=stage.value,
                provider=provider.provider_name,
                model=provider.model,
                prompt_version=REPORT_PROMPT_VERSION,
                schema_version=REPORT_SCHEMA_VERSION,
                input_tokens=None,
                output_tokens=None,
                finish_reason=None,
                error_message=str(exc),
            )
            self._write_diagnostic(diagnostic_dir / "error.json", {"error": str(exc)})
            raise ProviderCallError(f"LLM call failed at stage {stage.value}: {exc}") from exc
        run_repository.record_generation_llm_call(
            self.database_path,
            run_id,
            call_id=call_id,
            stage=stage.value,
            provider=provider.provider_name,
            model=provider.model,
            prompt_version=REPORT_PROMPT_VERSION,
            schema_version=REPORT_SCHEMA_VERSION,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            finish_reason=response.finish_reason,
        )
        self._write_diagnostic(
            diagnostic_dir / "response.json",
            {
                "content": response.content,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "finish_reason": response.finish_reason,
            },
        )
        return response

    def _persist(
        self,
        record: CollectionReportRecord,
        run_id: str,
        snapshot: SourceSnapshot,
        report: CollectionReport,
        instructions: str,
        summaries: list[tuple[str, StructuredSummary, dict[str, object]]],
        synthesis_reused: bool,
    ) -> tuple[CollectionArtifact, ...]:
        root = self.data_dir / "collections" / "reports" / record.id
        input_payload = (
            json.dumps(
                {
                    "kind": record.kind.value,
                    "user_prompt": record.user_prompt,
                    "instructions": instructions,
                    "synthesis_reused": synthesis_reused,
                    "sources": json.loads(_sources_payload(summaries, None)),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        payloads = {
            "report_json": (root / "report.json", render_report_json(report)),
            "report_markdown": (
                root / "report.md",
                render_report_markdown(report).encode("utf-8"),
            ),
            "report_source": (
                root / "source.json",
                (
                    json.dumps(
                        snapshot.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            ),
            "report_input": (root / "input.json", input_payload),
        }
        writes: list[repository.ArtifactWrite] = []
        written: list[Path] = []
        try:
            for kind, (path, content) in payloads.items():
                atomic_write_bytes(path, content, prefix="collection-")
                written.append(path)
                writes.append(
                    repository.ArtifactWrite(
                        kind=kind,
                        path=path.relative_to(self.data_dir).as_posix(),
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    )
                )
            return repository.save_report_completion(
                self.database_path,
                report_id=record.id,
                run_id=run_id,
                version=REPORT_SCHEMA_VERSION,
                source_fingerprint=record.source_fingerprint,
                artifacts=tuple(writes),
            )
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise

    def _read_report(self, artifacts: tuple[CollectionArtifact, ...]) -> CollectionReport:
        artifact = next(item for item in artifacts if item.kind == "report_json")
        try:
            content = (self.data_dir / artifact.path).read_bytes()
            if hashlib.sha256(content).hexdigest() != artifact.sha256:
                raise ScopeError("Stored collection report hash does not match its index")
            return CollectionReport.model_validate_json(content)
        except (OSError, ValidationError) as exc:
            raise ScopeError(f"Cannot load collection report: {exc}") from exc

    @staticmethod
    def _write_diagnostic(path: Path, payload: dict[str, object]) -> None:
        atomic_write_bytes(
            path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
            prefix="collection-",
        )


def _report_schema() -> str:
    return json.dumps(CollectionReport.model_json_schema(), ensure_ascii=False, indent=2)


def _sources_payload(
    summaries: list[tuple[str, StructuredSummary, dict[str, object]]],
    synthesis: CollectionSynthesis | None,
) -> str:
    documents: list[dict[str, object]] = []
    if synthesis is not None:
        documents.append({"collection_synthesis": synthesis.model_dump(mode="json")})
    documents.extend(source for _paper_id, _summary, source in summaries)
    return json.dumps(documents, ensure_ascii=False, separators=(",", ":"))


def _instructions(kind: ReportKind, user_prompt: str | None) -> str:
    base = _KIND_INSTRUCTIONS[kind]
    if kind is ReportKind.CUSTOM and user_prompt:
        return base + user_prompt
    return base


def _report_title(kind: ReportKind, snapshot: SourceSnapshot, user_prompt: str | None) -> str:
    name = snapshot.collection.name if snapshot.collection is not None else "collection"
    if kind is ReportKind.CUSTOM and user_prompt:
        prompt_title = " ".join(user_prompt.split())[:60].rstrip()
        if prompt_title:
            return f"{_KIND_TITLES[kind]}: {prompt_title}"
    return f"{_KIND_TITLES[kind]}: {name}"
