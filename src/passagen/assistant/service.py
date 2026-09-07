"""Persistent paper conversation service.

Implements the single-paper question-answering flow: persist the user message,
rewrite the question, plan context deterministically, retrieve raw sections,
generate a structured answer, validate citations with one bounded repair, and
atomically persist the turn. Failures keep the question visible and never
persist half an answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

from pydantic import ValidationError

from passagen.assistant import repository
from passagen.assistant.citations import PaperEvidenceIndex, validate_answer_citations
from passagen.assistant.context import (
    AssembledContext,
    build_context,
    raw_section_token_cap,
)
from passagen.assistant.errors import (
    AnswerValidationError,
    AssistantError,
    AssistantNotFoundError,
    CitationValidationError,
    ContextPlanError,
    ProviderCallError,
    ScopeError,
)
from passagen.assistant.models import AssistantTurn, ConversationDetail, TurnSubmission
from passagen.assistant.planner import (
    RewriteResult,
    deterministic_plan,
    normalize_question,
    question_hash,
)
from passagen.assistant.retrieval import InMemorySectionRetrieval, RetrievedSection
from passagen.assistant.schemas import (
    ContextPlan,
    ContextSource,
    Conversation,
    ConversationScope,
    GenerationRunKind,
    GenerationStage,
    Message,
    MessageRole,
    MessageStatus,
    PaperSourceSnapshot,
    QaRecord,
    SourceSnapshot,
    StructuredAnswer,
    source_fingerprint,
)
from passagen.assistant.snapshots import build_paper_snapshot
from passagen.assistant.versions import ANSWER_SCHEMA_VERSION, QA_PROMPT_VERSION
from passagen.config import LlmSettings
from passagen.external.llm import LlmProvider, LlmProviderError, LlmResponse
from passagen.parsing import ParsedPaper
from passagen.prompting.templates import QaPromptTemplates, load_qa_prompt_templates
from passagen.providers.budget import TokenBudget
from passagen.providers.llm import OpenAICompatibleProvider, retry_truncated_response
from passagen.stages.summarization.schema import StructuredSummary
from passagen.storage.files import atomic_write_bytes
from passagen.storage.repository import get_artifact

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 10
_MAX_RAW_SECTIONS = 3
_REPAIR_ERROR_RESERVE_TOKENS = 512


class ConversationService:
    """Application service for persistent paper conversations."""

    def __init__(
        self,
        database_path: Path,
        data_dir: Path,
        settings: LlmSettings,
        *,
        provider: LlmProvider | None = None,
        answer_max_output_tokens: int = 4096,
        rewrite_max_output_tokens: int = 1024,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.data_dir = data_dir.expanduser().resolve()
        self.settings = settings
        self.provider = provider
        self.answer_max_output_tokens = answer_max_output_tokens
        self.rewrite_max_output_tokens = rewrite_max_output_tokens
        self.budget = TokenBudget.from_settings(settings)

    def create_conversation(self, paper_id: str, *, title: str | None = None) -> Conversation:
        return repository.create_conversation(
            self.database_path,
            paper_id=paper_id,
            title=title or "New conversation",
        )

    def list_conversations(self, paper_id: str) -> tuple[Conversation, ...]:
        return repository.list_conversations(self.database_path, paper_id=paper_id)

    def get_conversation(self, conversation_id: str) -> ConversationDetail:
        conversation = repository.get_conversation(self.database_path, conversation_id)
        if conversation is None:
            raise AssistantNotFoundError(f"Conversation not found: {conversation_id}")
        messages = repository.list_messages(self.database_path, conversation_id)
        return ConversationDetail(conversation, messages)

    def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        if not title.strip():
            raise ScopeError("Conversation title must not be blank")
        try:
            return repository.rename_conversation(
                self.database_path, conversation_id, title.strip()
            )
        except repository.ConversationNotFoundError as exc:
            raise AssistantNotFoundError(f"Conversation not found: {conversation_id}") from exc

    def delete_conversation(self, conversation_id: str) -> None:
        repository.delete_conversation(self.database_path, conversation_id)

    def get_qa_record(self, qa_record_id: str) -> QaRecord:
        record = repository.get_qa_record(self.database_path, qa_record_id)
        if record is None:
            raise AssistantNotFoundError(f"QA record not found: {qa_record_id}")
        return record

    def archive_qa_record(
        self, qa_record_id: str, *, title: str, tags: list[str] | None = None
    ) -> QaRecord:
        if not title.strip():
            raise ScopeError("Archive title must not be blank")
        clean_tags = [tag.strip() for tag in tags or [] if tag.strip()]
        try:
            return repository.update_qa_record_archive(
                self.database_path,
                qa_record_id,
                archived=True,
                title=title.strip(),
                tags=clean_tags,
            )
        except repository.QaRecordNotFoundError as exc:
            raise AssistantNotFoundError(f"QA record not found: {qa_record_id}") from exc

    def unarchive_qa_record(self, qa_record_id: str) -> QaRecord:
        try:
            return repository.update_qa_record_archive(
                self.database_path, qa_record_id, archived=False
            )
        except repository.QaRecordNotFoundError as exc:
            raise AssistantNotFoundError(f"QA record not found: {qa_record_id}") from exc

    def search_qa_records(
        self,
        query: str | None = None,
        *,
        archived: bool | None = None,
        paper_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QaRecord, ...]:
        return repository.search_qa_records(
            self.database_path, query=query, archived=archived, paper_id=paper_id, limit=limit
        )

    def claim_next_queued_run(self) -> repository.GenerationRunRecord | None:
        return repository.claim_next_queued_run(self.database_path)

    def get_generation_run(self, run_id: str) -> repository.GenerationRunRecord:
        run = repository.get_generation_run(self.database_path, run_id)
        if run is None:
            raise AssistantNotFoundError(f"Generation run not found: {run_id}")
        return run

    def find_generation_run(self, run_id: str) -> repository.GenerationRunRecord | None:
        return repository.get_generation_run(self.database_path, run_id)

    def find_qa_record_for_message(self, answer_message_id: str) -> QaRecord | None:
        return repository.get_qa_record_by_answer_message(self.database_path, answer_message_id)

    def list_generation_llm_calls(
        self, run_id: str
    ) -> tuple[repository.GenerationLlmCallRecord, ...]:
        self.get_generation_run(run_id)
        return repository.list_generation_llm_calls(self.database_path, run_id)

    def interrupt_active_runs(self) -> int:
        return repository.interrupt_active_generation_runs(self.database_path)

    def ask(self, conversation_id: str, question: str) -> AssistantTurn:
        submission = self.submit_turn(conversation_id, question)
        return self.execute_turn(submission.run_id)

    def submit_turn(self, conversation_id: str, question: str) -> TurnSubmission:
        """Persist the user question and queue an answer run without executing it."""

        if not question.strip():
            raise ScopeError("Question must not be blank")
        conversation = repository.get_conversation(self.database_path, conversation_id)
        if conversation is None:
            raise AssistantNotFoundError(f"Conversation not found: {conversation_id}")
        if conversation.scope is not ConversationScope.PAPER or conversation.paper_id is None:
            raise ScopeError("Collection conversations are not supported yet")
        snapshot = build_paper_snapshot(self.database_path, conversation.paper_id)
        run_id, question_message, answer_message = repository.create_turn_submission(
            self.database_path,
            kind=GenerationRunKind.ANSWER.value,
            paper_id=conversation.paper_id,
            conversation_id=conversation_id,
            source_snapshot_json=snapshot.model_dump_json(),
            question=question.strip(),
        )
        return TurnSubmission(conversation_id, run_id, question_message, answer_message)

    def execute_turn(self, run_id: str) -> AssistantTurn:
        """Execute a queued or claimed answer run; used directly and by the worker."""

        run = repository.start_generation_run(self.database_path, run_id)
        if run.status != "running" or run.conversation_id is None:
            raise ScopeError(f"Generation run {run_id} is not executable (status={run.status})")
        conversation = repository.get_conversation(self.database_path, run.conversation_id)
        if conversation is None:
            raise AssistantNotFoundError(f"Conversation not found: {run.conversation_id}")
        answer_message = repository.get_message_by_run(self.database_path, run_id)
        if answer_message is None:
            raise AssistantNotFoundError(f"No pending answer message for run {run_id}")
        question_message = self._question_message_for(run.conversation_id, answer_message.id)
        if run.source_snapshot_json is None:
            raise ScopeError(f"Generation run {run_id} has no source snapshot")
        try:
            snapshot = SourceSnapshot.model_validate_json(run.source_snapshot_json)
            record = self._run_turn(
                conversation, snapshot, run_id, question_message, answer_message
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, AssistantError) else "internal_error"
            repository.fail_turn(
                self.database_path,
                answer_message_id=answer_message.id,
                run_id=run_id,
                error_code=code,
                error_message=str(exc),
            )
            raise
        completed = repository.list_messages(self.database_path, run.conversation_id)
        by_id = {message.id: message for message in completed}
        return AssistantTurn(
            conversation_id=run.conversation_id,
            run_id=run_id,
            question_message=by_id[question_message.id],
            answer_message=by_id[answer_message.id],
            qa_record=record,
        )

    def _question_message_for(self, conversation_id: str, answer_message_id: str) -> Message:
        messages = repository.list_messages(self.database_path, conversation_id)
        for index, message in enumerate(messages):
            if message.id == answer_message_id and index > 0:
                candidate = messages[index - 1]
                if candidate.role is MessageRole.USER:
                    return candidate
                break
        raise AssistantNotFoundError(f"No user question found before message {answer_message_id}")

    def _run_turn(
        self,
        conversation: Conversation,
        snapshot: SourceSnapshot,
        run_id: str,
        question_message: Message,
        answer_message: Message,
    ) -> QaRecord:
        paper = _require_paper_snapshot(snapshot)
        prompts = load_qa_prompt_templates()
        history = self._recent_history(conversation.id, before_id=question_message.id)
        rewrite = self._rewrite(prompts, run_id, question_message.content, history)
        available = _available_sources(paper)
        plan = deterministic_plan(
            standalone_question=rewrite.standalone_question,
            retrieval_queries=rewrite.retrieval_queries,
            requires_exact_quote=rewrite.requires_exact_quote,
            has_history=bool(history),
            paper_id=paper.paper_id,
            available_sources=available,
        )
        evidence = self._load_evidence(paper, plan)
        base_prompt_tokens = max(
            self.budget.estimate_tokens(
                prompts.answer.content + _answer_schema() + plan.standalone_question
            ),
            self.budget.estimate_tokens(
                prompts.repair.content + _answer_schema() + plan.standalone_question
            ),
        )
        overhead = base_prompt_tokens + self.answer_max_output_tokens + _REPAIR_ERROR_RESERVE_TOKENS
        sections = self._retrieve(paper, plan, evidence.parsed, overhead)
        context = build_context(
            snapshot=paper,
            plan=plan,
            history=history,
            summary=evidence.summary,
            outline=evidence.outline,
            sections=sections,
            budget=self.budget,
            reserved_output_tokens=self.answer_max_output_tokens,
            prompt_overhead_tokens=overhead,
        )
        answer = self._generate_answer(prompts, run_id, plan, context, evidence)
        normalized = normalize_question(rewrite.standalone_question)
        record = QaRecord(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            question_message_id=question_message.id,
            answer_message_id=answer_message.id,
            standalone_question=rewrite.standalone_question,
            normalized_question=normalized,
            normalized_question_hash=question_hash(normalized),
            intent=plan.intent,
            context_plan=plan,
            answer=answer,
            source_snapshot=snapshot,
            source_fingerprint=source_fingerprint(snapshot),
            prompt_version=QA_PROMPT_VERSION,
            answer_schema_version=ANSWER_SCHEMA_VERSION,
            created_at=answer_message.created_at,
        )
        repository.save_qa_turn(
            self.database_path,
            record=record,
            answer_content=answer.answer_markdown,
            run_id=run_id,
        )
        return record

    def _recent_history(self, conversation_id: str, *, before_id: str) -> list[Message]:
        messages = repository.list_messages(self.database_path, conversation_id)
        before_index = next(
            (index for index, message in enumerate(messages) if message.id == before_id),
            len(messages),
        )
        history = [
            message
            for message in messages[:before_index]
            if message.status is MessageStatus.COMPLETED
        ]
        return history[-_HISTORY_LIMIT:]

    def _rewrite(
        self,
        prompts: QaPromptTemplates,
        run_id: str,
        question: str,
        history: list[Message],
    ) -> RewriteResult:
        history_text = "\n".join(f"{m.role.value}: {m.content}" for m in history) or "(empty)"
        prompt = prompts.rewrite.render(
            schema=_rewrite_schema(), history=history_text, question=question
        )
        response = self._call_llm(
            run_id,
            GenerationStage.REWRITE,
            prompt,
            max_tokens=self.rewrite_max_output_tokens,
        )
        try:
            result = RewriteResult.model_validate_json(response.content)
        except ValidationError as exc:
            raise ContextPlanError(f"Question rewrite failed schema validation: {exc}") from exc
        if not result.standalone_question.strip():
            raise ContextPlanError("Question rewrite returned a blank standalone question")
        return result

    def _load_evidence(self, paper: PaperSourceSnapshot, plan: ContextPlan) -> PaperEvidenceIndex:
        summary = None
        if ContextSource.SUMMARY in plan.sources:
            summary = self._load_summary(paper.paper_id)
        outline = None
        if ContextSource.OUTLINE in plan.sources:
            outline = self._load_outline(paper.paper_id)
        parsed = None
        if ContextSource.RAW in plan.sources:
            parsed = self._load_parsed(paper.paper_id)
        return PaperEvidenceIndex(snapshot=paper, summary=summary, outline=outline, parsed=parsed)

    def _load_summary(self, paper_id: str) -> StructuredSummary:
        artifact = get_artifact(self.database_path, paper_id, "summary_json")
        if artifact is None:
            raise ScopeError(f"Paper {paper_id} has no summary_json artifact")
        try:
            return StructuredSummary.model_validate_json(
                (self.data_dir / artifact.path).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise ScopeError(f"Cannot load summary for paper {paper_id}: {exc}") from exc

    def _load_outline(self, paper_id: str) -> str:
        artifact = get_artifact(self.database_path, paper_id, "outline_md")
        if artifact is None:
            raise ContextPlanError(f"Paper {paper_id} has no outline_md artifact")
        try:
            return (self.data_dir / artifact.path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ContextPlanError(f"Cannot load outline for paper {paper_id}: {exc}") from exc

    def _load_parsed(self, paper_id: str) -> ParsedPaper:
        artifact = get_artifact(self.database_path, paper_id, "extracted_json")
        if artifact is None:
            raise ScopeError(f"Paper {paper_id} has no extracted_json artifact")
        try:
            return ParsedPaper.model_validate_json(
                (self.data_dir / artifact.path).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise ScopeError(f"Cannot load parsed paper {paper_id}: {exc}") from exc

    def _retrieve(
        self,
        paper: PaperSourceSnapshot,
        plan: ContextPlan,
        parsed: ParsedPaper | None,
        prompt_overhead_tokens: int,
    ) -> list[RetrievedSection]:
        if ContextSource.RAW not in plan.sources:
            return []
        if parsed is None:
            raise ContextPlanError("the plan requires raw sections but they were not loaded")
        artifact = next(a for a in paper.artifacts if a.kind == "extracted_json")
        retrieval = InMemorySectionRetrieval(
            paper.paper_id,
            parsed,
            artifact_sha256=artifact.sha256,
            chars_per_token=self.settings.chars_per_token,
        )
        return retrieval.search(
            plan.retrieval_queries,
            max_sections=_MAX_RAW_SECTIONS,
            max_tokens=raw_section_token_cap(
                self.budget, self.answer_max_output_tokens, prompt_overhead_tokens
            ),
        )

    def _generate_answer(
        self,
        prompts: QaPromptTemplates,
        run_id: str,
        plan: ContextPlan,
        context: AssembledContext,
        evidence: PaperEvidenceIndex,
    ) -> StructuredAnswer:
        prompt = prompts.answer.render(
            schema=_answer_schema(),
            question=plan.standalone_question,
            context=context.render(),
        )

        def request(attempt: int, attempt_tokens: int) -> LlmResponse:
            return self._call_llm(run_id, GenerationStage.ANSWER, prompt, max_tokens=attempt_tokens)

        def is_valid(content: str) -> bool:
            try:
                StructuredAnswer.model_validate_json(content)
            except ValidationError:
                return False
            return True

        response = retry_truncated_response(
            request,
            initial_max_tokens=self.answer_max_output_tokens,
            is_valid=is_valid,
        )
        try:
            answer = self._canonicalize_answer(self._parse_answer(response.content), plan)
            validate_answer_citations(answer, evidence)
            return answer
        except (CitationValidationError, AnswerValidationError) as exc:
            return self._repair_answer(
                prompts, run_id, response.content, str(exc), plan, context, evidence
            )

    def _repair_answer(
        self,
        prompts: QaPromptTemplates,
        run_id: str,
        candidate_content: str,
        validation_error: str,
        plan: ContextPlan,
        context: AssembledContext,
        evidence: PaperEvidenceIndex,
    ) -> StructuredAnswer:
        prompt = prompts.repair.render(
            schema=_answer_schema(),
            question=plan.standalone_question,
            context=context.render(),
            validation_error=validation_error,
            candidate=candidate_content,
        )
        response = self._call_llm(
            run_id, GenerationStage.REPAIR, prompt, max_tokens=self.answer_max_output_tokens
        )
        repaired = self._canonicalize_answer(self._parse_answer(response.content), plan)
        try:
            validate_answer_citations(repaired, evidence)
        except (CitationValidationError, AnswerValidationError) as exc:
            raise AnswerValidationError(
                f"Answer failed validation after one repair: {exc}"
            ) from exc
        return repaired

    @staticmethod
    def _canonicalize_answer(answer: StructuredAnswer, plan: ContextPlan) -> StructuredAnswer:
        return answer.model_copy(
            update={"standalone_question": plan.standalone_question, "intent": plan.intent}
        )

    def _parse_answer(self, content: str) -> StructuredAnswer:
        try:
            return StructuredAnswer.model_validate_json(content)
        except ValidationError as exc:
            detail = exc.json(include_input=False)
            raise AnswerValidationError(f"Answer failed schema validation: {detail}") from exc

    def _call_llm(
        self,
        run_id: str,
        stage: GenerationStage,
        prompt: str,
        *,
        max_tokens: int,
    ) -> LlmResponse:
        if not self.budget.fits(prompt, max_tokens):
            raise ContextPlanError(
                f"{stage.value} prompt exceeds the configured LLM context budget"
            )
        provider = self.provider
        if provider is None:
            provider = OpenAICompatibleProvider(self.settings)
        call_id = str(uuid.uuid4())
        diagnostic_dir = self.data_dir / "runs" / run_id / "llm" / call_id
        self._write_diagnostic(
            diagnostic_dir / "request.json",
            {
                "stage": stage.value,
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
            repository.record_generation_llm_call(
                self.database_path,
                run_id,
                call_id=call_id,
                stage=stage.value,
                provider=provider.provider_name,
                model=provider.model,
                prompt_version=QA_PROMPT_VERSION,
                schema_version=ANSWER_SCHEMA_VERSION,
                input_tokens=None,
                output_tokens=None,
                finish_reason=None,
                error_message=str(exc),
            )
            self._write_diagnostic(diagnostic_dir / "error.json", {"error": str(exc)})
            raise ProviderCallError(f"LLM call failed at stage {stage.value}: {exc}") from exc
        repository.record_generation_llm_call(
            self.database_path,
            run_id,
            call_id=call_id,
            stage=stage.value,
            provider=provider.provider_name,
            model=provider.model,
            prompt_version=QA_PROMPT_VERSION,
            schema_version=ANSWER_SCHEMA_VERSION,
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

    @staticmethod
    def _write_diagnostic(path: Path, payload: dict[str, object]) -> None:
        atomic_write_bytes(
            path, json.dumps(payload, ensure_ascii=False, indent=2).encode(), prefix="qa-"
        )


def _require_paper_snapshot(snapshot: SourceSnapshot) -> PaperSourceSnapshot:
    if snapshot.paper is None:
        raise ScopeError("the source snapshot is not paper-scoped")
    return snapshot.paper


def _available_sources(paper: PaperSourceSnapshot) -> set[ContextSource]:
    sources = {ContextSource.CONVERSATION, ContextSource.PREVIOUS_QA}
    kinds = {artifact.kind for artifact in paper.artifacts}
    if "summary_json" in kinds:
        sources.add(ContextSource.SUMMARY)
    if "outline_md" in kinds:
        sources.add(ContextSource.OUTLINE)
    if "extracted_json" in kinds:
        sources.add(ContextSource.RAW)
    return sources


def _rewrite_schema() -> str:
    return json.dumps(RewriteResult.model_json_schema(), ensure_ascii=False, indent=2)


def _answer_schema() -> str:
    return json.dumps(StructuredAnswer.model_json_schema(), ensure_ascii=False, indent=2)
