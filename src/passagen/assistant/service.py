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
import re
import uuid
from pathlib import Path

from pydantic import ValidationError

from passagen.assistant import repository
from passagen.assistant.citations import (
    EvidenceIndex,
    PaperEvidenceIndex,
    validate_answer_citations,
)
from passagen.assistant.context import (
    AssembledContext,
    build_collection_context,
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
    StaleSourceError,
)
from passagen.assistant.models import (
    AssistantTurn,
    ConversationDetail,
    QaRecordView,
    TurnSubmission,
)
from passagen.assistant.planner import (
    QaSemanticDecision,
    QaSemanticRelation,
    RewriteResult,
    collection_deterministic_plan,
    deterministic_plan,
    normalize_question,
    question_hash,
)
from passagen.assistant.retrieval import (
    CollectionFtsSectionRetrieval,
    FtsSectionRetrieval,
    PaperCandidate,
    RetrievedSection,
    select_papers,
)
from passagen.assistant.schemas import (
    DEFAULT_CONVERSATION_TITLE,
    CollectionSourceSnapshot,
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
    ReusePolicy,
    SourceSnapshot,
    SourceStatus,
    StructuredAnswer,
    TurnDisposition,
    source_fingerprint,
)
from passagen.assistant.snapshots import (
    build_collection_qa_snapshot,
    build_paper_snapshot,
    load_synthesis_text,
)
from passagen.assistant.versions import ANSWER_SCHEMA_VERSION, QA_PROMPT_VERSION
from passagen.config import AssistantSettings, LlmPurpose, LlmSettings
from passagen.external.llm import LlmProvider, LlmProviderError, LlmResponse
from passagen.parsing import ParsedPaper
from passagen.prompting.templates import QaPromptTemplates, load_qa_prompt_templates
from passagen.providers.budget import TokenBudget
from passagen.providers.llm import resolve_llm_provider, retry_truncated_response
from passagen.stages.summarization.schema import StructuredSummary
from passagen.storage.files import atomic_write_bytes
from passagen.storage.repository import (
    get_artifact,
    paper_sections_artifact_sha,
    replace_paper_sections,
)

logger = logging.getLogger(__name__)

_REPAIR_ERROR_RESERVE_TOKENS = 512


class ConversationService:
    """Application service for persistent paper conversations."""

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
        _answer_profile_name, answer_profile = settings.resolve(LlmPurpose.QA_ANSWER)
        self.budget = TokenBudget.from_settings(answer_profile)

    def create_conversation(
        self,
        paper_id: str | None = None,
        *,
        collection_id: str | None = None,
        title: str | None = None,
    ) -> Conversation:
        if (paper_id is None) == (collection_id is None):
            raise ScopeError("A conversation requires exactly one of paper_id or collection_id")
        return repository.create_conversation(
            self.database_path,
            paper_id=paper_id,
            collection_id=collection_id,
            title=title or DEFAULT_CONVERSATION_TITLE,
        )

    def list_conversations(
        self, paper_id: str | None = None, *, collection_id: str | None = None
    ) -> tuple[Conversation, ...]:
        if (paper_id is None) == (collection_id is None):
            raise ScopeError(
                "Listing conversations requires exactly one of paper_id or collection_id"
            )
        return repository.list_conversations(
            self.database_path, paper_id=paper_id, collection_id=collection_id
        )

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

    def get_qa_record_view(self, qa_record_id: str) -> QaRecordView:
        record = self.get_qa_record(qa_record_id)
        return QaRecordView(record, self.source_status(record))

    def source_status(self, record: QaRecord) -> SourceStatus:
        if record.source_snapshot.paper is None and record.source_snapshot.collection is None:
            return SourceStatus(stale=True, reasons=["unsupported_scope"])
        try:
            current = self._current_snapshot(record.source_snapshot)
        except ScopeError:
            return SourceStatus(stale=True, reasons=["source_missing"])
        reasons = _snapshot_changes(record.source_snapshot, current)
        return SourceStatus(stale=bool(reasons), reasons=reasons)

    def _current_snapshot(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if snapshot.paper is not None:
            return build_paper_snapshot(self.database_path, snapshot.paper.paper_id)
        if snapshot.collection is not None:
            return build_collection_qa_snapshot(
                self.database_path, self.data_dir, snapshot.collection.collection_id
            )
        raise ScopeError("the source snapshot has no paper or collection scope")

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
        collection_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QaRecord, ...]:
        return repository.search_qa_records(
            self.database_path,
            query=query,
            archived=archived,
            paper_id=paper_id,
            collection_id=collection_id,
            limit=limit,
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

    def ask(
        self, conversation_id: str, question: str, *, force_regenerate: bool = False
    ) -> AssistantTurn:
        submission = self.submit_turn(conversation_id, question, force_regenerate=force_regenerate)
        return self.execute_turn(submission.run_id)

    def submit_turn(
        self, conversation_id: str, question: str, *, force_regenerate: bool = False
    ) -> TurnSubmission:
        """Persist the user question and queue an answer run without executing it."""

        if not question.strip():
            raise ScopeError("Question must not be blank")
        conversation = repository.get_conversation(self.database_path, conversation_id)
        if conversation is None:
            raise AssistantNotFoundError(f"Conversation not found: {conversation_id}")
        if conversation.scope is ConversationScope.PAPER and conversation.paper_id is not None:
            snapshot = build_paper_snapshot(self.database_path, conversation.paper_id)
            paper_id: str | None = conversation.paper_id
            collection_id: str | None = None
        elif (
            conversation.scope is ConversationScope.COLLECTION
            and conversation.collection_id is not None
        ):
            snapshot = build_collection_qa_snapshot(
                self.database_path, self.data_dir, conversation.collection_id
            )
            paper_id = None
            collection_id = conversation.collection_id
        else:
            raise ScopeError(f"Conversation {conversation_id} has an unsupported scope")
        run_id, question_message, answer_message = repository.create_turn_submission(
            self.database_path,
            kind=GenerationRunKind.ANSWER.value,
            paper_id=paper_id,
            collection_id=collection_id,
            conversation_id=conversation_id,
            source_snapshot_json=snapshot.model_dump_json(),
            question=question.strip(),
            reuse_policy=(ReusePolicy.FORCE_REGENERATE if force_regenerate else ReusePolicy.AUTO),
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
            record, disposition = self._run_turn(
                conversation,
                snapshot,
                run_id,
                question_message,
                answer_message,
                reuse_policy=run.reuse_policy,
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
            disposition=disposition,
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
        *,
        reuse_policy: ReusePolicy,
    ) -> tuple[QaRecord, TurnDisposition]:
        current_snapshot = self._current_snapshot(snapshot)
        if source_fingerprint(current_snapshot) != source_fingerprint(snapshot):
            raise StaleSourceError(
                f"{snapshot.scope.value.capitalize()} sources changed after this run was submitted"
            )
        prompts = load_qa_prompt_templates(
            rewrite_path=self.assistant_settings.rewrite_prompt_path,
            equivalence_path=self.assistant_settings.equivalence_prompt_path,
            answer_path=self.assistant_settings.answer_prompt_path,
            repair_path=self.assistant_settings.repair_prompt_path,
        )
        history = self._recent_history(conversation.id, before_id=question_message.id)
        rewrite = self._rewrite(prompts, run_id, question_message.content, history)
        normalized = normalize_question(rewrite.standalone_question)
        normalized_hash = question_hash(normalized)
        semantic_candidate: QaRecord | None = None
        semantic_relation: QaSemanticRelation | None = None
        if reuse_policy is ReusePolicy.AUTO:
            reused = self._reuse_exact(
                conversation,
                snapshot,
                rewrite,
                normalized,
                normalized_hash,
                run_id,
                question_message,
                answer_message,
            )
            if reused is not None:
                return reused, TurnDisposition.EXACT_REUSE
            semantic_candidate, semantic_relation = self._semantic_candidate(
                prompts,
                run_id,
                snapshot,
                rewrite.standalone_question,
                normalized_hash,
            )
            if (
                semantic_candidate is not None
                and semantic_relation is QaSemanticRelation.EQUIVALENT
            ):
                reused = self._reuse_record(
                    conversation,
                    snapshot,
                    rewrite,
                    normalized,
                    normalized_hash,
                    run_id,
                    question_message,
                    answer_message,
                    semantic_candidate,
                )
                return reused, TurnDisposition.SEMANTIC_REUSE
        if snapshot.paper is not None:
            plan = self._paper_plan(snapshot.paper, rewrite, history)
        elif snapshot.collection is not None:
            plan = self._collection_plan(snapshot.collection, rewrite, history)
        else:
            raise ScopeError("the source snapshot has no paper or collection scope")
        if semantic_candidate is not None and semantic_relation is QaSemanticRelation.PARTIAL:
            plan = plan.model_copy(
                update={
                    "reuse_qa_id": semantic_candidate.id,
                    "sources": [*plan.sources, ContextSource.PREVIOUS_QA],
                }
            )
        previous_qa = (
            semantic_candidate if semantic_relation is QaSemanticRelation.PARTIAL else None
        )
        answer_max_tokens = self.assistant_settings.answer_max_output_tokens
        overhead = (
            self._prompt_overhead(prompts, plan) + answer_max_tokens + _REPAIR_ERROR_RESERVE_TOKENS
        )
        context: AssembledContext
        evidence: PaperEvidenceIndex | EvidenceIndex
        if snapshot.paper is not None:
            context, evidence = self._paper_context(
                snapshot.paper, plan, history, previous_qa, overhead, answer_max_tokens
            )
        else:
            collection = _require_collection_snapshot(snapshot)
            context, evidence = self._collection_context(
                collection, plan, history, previous_qa, overhead, answer_max_tokens
            )
        answer = self._generate_answer(prompts, run_id, plan, context, evidence)
        record = QaRecord(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            question_message_id=question_message.id,
            answer_message_id=answer_message.id,
            standalone_question=rewrite.standalone_question,
            normalized_question=normalized,
            normalized_question_hash=normalized_hash,
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
            conversation_title=_conversation_title(rewrite.conversation_title, conversation),
        )
        return record, TurnDisposition.GENERATED

    def _prompt_overhead(self, prompts: QaPromptTemplates, plan: ContextPlan) -> int:
        return max(
            self.budget.estimate_tokens(
                prompts.answer.content + _answer_schema() + plan.standalone_question
            ),
            self.budget.estimate_tokens(
                prompts.repair.content + _answer_schema() + plan.standalone_question
            ),
        )

    def _paper_plan(
        self,
        paper: PaperSourceSnapshot,
        rewrite: RewriteResult,
        history: list[Message],
    ) -> ContextPlan:
        return deterministic_plan(
            standalone_question=rewrite.standalone_question,
            retrieval_queries=rewrite.retrieval_queries,
            requires_exact_quote=rewrite.requires_exact_quote,
            has_history=bool(history),
            paper_id=paper.paper_id,
            available_sources=_available_sources(paper),
        )

    def _collection_plan(
        self,
        collection: CollectionSourceSnapshot,
        rewrite: RewriteResult,
        history: list[Message],
    ) -> ContextPlan:
        return collection_deterministic_plan(
            standalone_question=rewrite.standalone_question,
            retrieval_queries=rewrite.retrieval_queries,
            requires_exact_quote=rewrite.requires_exact_quote,
            has_history=bool(history),
            available_sources=_available_collection_sources(collection),
        )

    def _paper_context(
        self,
        paper: PaperSourceSnapshot,
        plan: ContextPlan,
        history: list[Message],
        previous_qa: QaRecord | None,
        prompt_overhead_tokens: int,
        answer_max_tokens: int,
    ) -> tuple[AssembledContext, PaperEvidenceIndex]:
        evidence = self._load_evidence(paper, plan)
        sections = self._retrieve(paper, plan, evidence.parsed, prompt_overhead_tokens)
        context = build_context(
            snapshot=paper,
            plan=plan,
            history=history,
            previous_qa=previous_qa,
            summary=evidence.summary,
            outline=evidence.outline,
            sections=sections,
            budget=self.budget,
            reserved_output_tokens=answer_max_tokens,
            prompt_overhead_tokens=prompt_overhead_tokens,
        )
        return context, evidence

    def _collection_context(
        self,
        collection: CollectionSourceSnapshot,
        plan: ContextPlan,
        history: list[Message],
        previous_qa: QaRecord | None,
        prompt_overhead_tokens: int,
        answer_max_tokens: int,
    ) -> tuple[AssembledContext, EvidenceIndex]:
        summaries: dict[str, StructuredSummary] = {}
        for paper in collection.papers:
            if any(artifact.kind == "summary_json" for artifact in paper.artifacts):
                summaries[paper.paper_id] = self._load_summary(paper.paper_id)
        synthesis_text = (
            load_synthesis_text(self.database_path, self.data_dir, collection.synthesis)
            if collection.synthesis is not None
            else None
        )
        candidates = [
            PaperCandidate(
                paper_id=paper.paper_id,
                title=paper.title,
                text=_paper_scoring_text(paper, summaries.get(paper.paper_id), synthesis_text),
            )
            for paper in collection.papers
        ]
        selected = select_papers(
            candidates,
            plan.retrieval_queries or [plan.standalone_question],
            max_papers=self.assistant_settings.collection_max_selected_papers,
        )
        plan.paper_ids = selected
        parsed: dict[str, ParsedPaper] = {}
        sections: list[RetrievedSection] = []
        if ContextSource.RAW in plan.sources:
            paper_shas: dict[str, str] = {}
            for paper in collection.papers:
                if paper.paper_id not in selected:
                    continue
                artifact = next(
                    (item for item in paper.artifacts if item.kind == "extracted_json"), None
                )
                if artifact is None:
                    continue
                parsed_paper = self._load_parsed(paper.paper_id)
                parsed[paper.paper_id] = parsed_paper
                self._ensure_sections_index(paper.paper_id, artifact.sha256, parsed_paper)
                paper_shas[paper.paper_id] = artifact.sha256
            retrieval = CollectionFtsSectionRetrieval(
                self.database_path,
                paper_shas,
                chars_per_token=self.budget.chars_per_token,
            )
            sections = retrieval.search(
                plan.retrieval_queries,
                max_sections=self.assistant_settings.max_raw_sections,
                max_tokens=raw_section_token_cap(
                    self.budget,
                    answer_max_tokens,
                    prompt_overhead_tokens,
                ),
            )
        context = build_collection_context(
            snapshot=collection,
            plan=plan,
            history=history,
            previous_qa=previous_qa,
            synthesis_text=synthesis_text,
            summaries=summaries,
            sections=sections,
            budget=self.budget,
            reserved_output_tokens=answer_max_tokens,
            prompt_overhead_tokens=prompt_overhead_tokens,
        )
        by_id = {paper.paper_id: paper for paper in collection.papers}
        evidence = EvidenceIndex(
            papers=tuple(
                PaperEvidenceIndex(
                    snapshot=by_id[paper_id],
                    summary=summaries.get(paper_id),
                    outline=None,
                    parsed=parsed.get(paper_id),
                )
                for paper_id in selected
            )
        )
        return context, evidence

    def _reuse_exact(
        self,
        conversation: Conversation,
        snapshot: SourceSnapshot,
        rewrite: RewriteResult,
        normalized: str,
        normalized_hash: str,
        run_id: str,
        question_message: Message,
        answer_message: Message,
    ) -> QaRecord | None:
        candidates = repository.find_exact_qa_records(
            self.database_path,
            paper_id=snapshot.paper.paper_id if snapshot.paper is not None else None,
            collection_id=(
                snapshot.collection.collection_id if snapshot.collection is not None else None
            ),
            normalized_question=normalized,
            normalized_question_hash=normalized_hash,
        )
        fingerprint = source_fingerprint(snapshot)
        candidate = next(
            (
                item
                for item in candidates
                if self._candidate_is_compatible(item, snapshot, fingerprint)
            ),
            None,
        )
        if candidate is None:
            return None
        return self._reuse_record(
            conversation,
            snapshot,
            rewrite,
            normalized,
            normalized_hash,
            run_id,
            question_message,
            answer_message,
            candidate,
        )

    def _reuse_record(
        self,
        conversation: Conversation,
        snapshot: SourceSnapshot,
        rewrite: RewriteResult,
        normalized: str,
        normalized_hash: str,
        run_id: str,
        question_message: Message,
        answer_message: Message,
        candidate: QaRecord,
    ) -> QaRecord:
        fingerprint = source_fingerprint(snapshot)
        plan = ContextPlan(
            standalone_question=rewrite.standalone_question,
            intent=candidate.intent,
            reuse_qa_id=candidate.id,
            sources=[ContextSource.PREVIOUS_QA],
            paper_ids=list(snapshot.paper_ids()),
            answer_kind=candidate.context_plan.answer_kind,
        )
        answer = candidate.answer.model_copy(
            update={
                "standalone_question": rewrite.standalone_question,
                "intent": candidate.intent,
            }
        )
        record = QaRecord(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            question_message_id=question_message.id,
            answer_message_id=answer_message.id,
            standalone_question=rewrite.standalone_question,
            normalized_question=normalized,
            normalized_question_hash=normalized_hash,
            intent=candidate.intent,
            context_plan=plan,
            answer=answer,
            source_snapshot=snapshot,
            source_fingerprint=fingerprint,
            prompt_version=QA_PROMPT_VERSION,
            answer_schema_version=ANSWER_SCHEMA_VERSION,
            created_at=answer_message.created_at,
        )
        repository.save_qa_turn(
            self.database_path,
            record=record,
            answer_content=answer.answer_markdown,
            run_id=run_id,
            conversation_title=_conversation_title(rewrite.conversation_title, conversation),
        )
        return record

    def _semantic_candidate(
        self,
        prompts: QaPromptTemplates,
        run_id: str,
        snapshot: SourceSnapshot,
        question: str,
        normalized_hash: str,
    ) -> tuple[QaRecord | None, QaSemanticRelation | None]:
        fingerprint = source_fingerprint(self._current_snapshot(snapshot))
        pool = repository.find_qa_candidates(
            self.database_path,
            paper_id=snapshot.paper.paper_id if snapshot.paper is not None else None,
            collection_id=(
                snapshot.collection.collection_id if snapshot.collection is not None else None
            ),
            exclude_normalized_question_hash=normalized_hash,
            pool_size=self.assistant_settings.qa_candidate_pool_size,
        )
        compatible = [
            candidate
            for candidate in pool
            if self._candidate_is_compatible(candidate, snapshot, fingerprint)
        ]
        candidates = _rank_qa_candidates(question, compatible)[
            : self.assistant_settings.max_qa_candidates
        ]
        if not candidates:
            return None, None
        candidate_json = json.dumps(
            [
                {
                    "candidate_id": candidate.id,
                    "question": candidate.standalone_question,
                    "answer": candidate.answer.answer_markdown[:4_000],
                }
                for candidate in candidates
            ],
            ensure_ascii=False,
        )
        prompt = prompts.equivalence.render(
            schema=json.dumps(QaSemanticDecision.model_json_schema(), ensure_ascii=False, indent=2),
            question=question,
            candidates=candidate_json,
        )
        try:
            response = self._call_llm(
                run_id,
                GenerationStage.ROUTE,
                prompt,
                max_tokens=self.assistant_settings.equivalence_max_output_tokens,
            )
            decision = QaSemanticDecision.model_validate_json(response.content)
        except (ProviderCallError, ContextPlanError, ValidationError) as exc:
            logger.warning("Ignoring unusable QA semantic decision: %s", exc)
            return None, None
        by_id = {candidate.id: candidate for candidate in candidates}
        if set(match.candidate_id for match in decision.matches) != set(by_id):
            logger.warning("Ignoring QA semantic decision with incomplete candidate ids")
            return None, None
        for relation in (QaSemanticRelation.EQUIVALENT, QaSemanticRelation.PARTIAL):
            match = next(
                (
                    item
                    for item in decision.matches
                    if item.relation is relation and item.confidence == "high"
                ),
                None,
            )
            if match is not None:
                return by_id[match.candidate_id], relation
        return None, None

    def _candidate_is_compatible(
        self, candidate: QaRecord, snapshot: SourceSnapshot, fingerprint: str
    ) -> bool:
        if (
            candidate.source_fingerprint != fingerprint
            or candidate.prompt_version != QA_PROMPT_VERSION
            or candidate.answer_schema_version != ANSWER_SCHEMA_VERSION
        ):
            return False
        try:
            evidence = self._evidence_for_answer(snapshot, candidate.answer)
            validate_answer_citations(candidate.answer, evidence)
        except (AssistantError, OSError, ValidationError):
            return False
        return True

    def _evidence_for_answer(
        self, snapshot: SourceSnapshot, answer: StructuredAnswer
    ) -> PaperEvidenceIndex | EvidenceIndex:
        kinds = {citation.artifact_kind.value for citation in answer.citations}
        if snapshot.paper is not None:
            paper = snapshot.paper
            return PaperEvidenceIndex(
                snapshot=paper,
                summary=self._load_summary(paper.paper_id) if "summary_json" in kinds else None,
                outline=self._load_outline(paper.paper_id) if "outline_md" in kinds else None,
                parsed=self._load_parsed(paper.paper_id) if "extracted_json" in kinds else None,
            )
        if snapshot.collection is not None:
            by_id = {paper.paper_id: paper for paper in snapshot.collection.papers}
            papers: list[PaperEvidenceIndex] = []
            for paper_id in dict.fromkeys(citation.paper_id for citation in answer.citations):
                snapshot_paper = by_id.get(paper_id)
                if snapshot_paper is None:
                    raise ScopeError(
                        f"QA candidate cites paper {paper_id} outside the collection snapshot"
                    )
                papers.append(
                    PaperEvidenceIndex(
                        snapshot=snapshot_paper,
                        summary=(self._load_summary(paper_id) if "summary_json" in kinds else None),
                        outline=None,
                        parsed=self._load_parsed(paper_id) if "extracted_json" in kinds else None,
                    )
                )
            return EvidenceIndex(papers=tuple(papers))
        raise ScopeError("the source snapshot has no paper or collection scope")

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
        limit = self.assistant_settings.max_history_messages
        return history[-limit:] if limit else []

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
            max_tokens=self.assistant_settings.rewrite_max_output_tokens,
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
        self._ensure_sections_index(paper.paper_id, artifact.sha256, parsed)
        retrieval = FtsSectionRetrieval(
            self.database_path,
            paper.paper_id,
            artifact_sha256=artifact.sha256,
            chars_per_token=self.budget.chars_per_token,
        )
        return retrieval.search(
            plan.retrieval_queries,
            max_sections=self.assistant_settings.max_raw_sections,
            max_tokens=raw_section_token_cap(
                self.budget,
                self.assistant_settings.answer_max_output_tokens,
                prompt_overhead_tokens,
            ),
        )

    def _ensure_sections_index(
        self, paper_id: str, artifact_sha256: str, parsed: ParsedPaper
    ) -> None:
        """Materialize the FTS section index for one snapshot-pinned artifact."""

        if paper_sections_artifact_sha(self.database_path, paper_id) == artifact_sha256:
            return
        current = get_artifact(self.database_path, paper_id, "extracted_json")
        if current is None or current.sha256 != artifact_sha256:
            raise StaleSourceError(
                f"Paper {paper_id} extracted text changed after this run was submitted"
            )
        replace_paper_sections(self.database_path, paper_id, current, parsed.sections)

    def _generate_answer(
        self,
        prompts: QaPromptTemplates,
        run_id: str,
        plan: ContextPlan,
        context: AssembledContext,
        evidence: PaperEvidenceIndex | EvidenceIndex,
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
            initial_max_tokens=self.assistant_settings.answer_max_output_tokens,
            is_valid=is_valid,
            max_attempts=self.assistant_settings.truncated_response_max_attempts,
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
        evidence: PaperEvidenceIndex | EvidenceIndex,
    ) -> StructuredAnswer:
        prompt = prompts.repair.render(
            schema=_answer_schema(),
            question=plan.standalone_question,
            context=context.render(),
            validation_error=validation_error,
            candidate=candidate_content,
        )
        response = self._call_llm(
            run_id,
            GenerationStage.REPAIR,
            prompt,
            max_tokens=self.assistant_settings.answer_max_output_tokens,
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
        purpose = {
            GenerationStage.REWRITE: LlmPurpose.QA_REWRITE,
            GenerationStage.ROUTE: LlmPurpose.QA_EQUIVALENCE,
            GenerationStage.ANSWER: LlmPurpose.QA_ANSWER,
            GenerationStage.REPAIR: LlmPurpose.QA_REPAIR,
        }[stage]
        resolved = resolve_llm_provider(self.settings, purpose, self.provider)
        budget = TokenBudget.from_settings(resolved.settings)
        if not budget.fits(prompt, max_tokens):
            raise ContextPlanError(
                f"{stage.value} prompt exceeds the configured LLM context budget"
            )
        provider = resolved.provider
        call_id = str(uuid.uuid4())
        diagnostic_dir = self.data_dir / "runs" / run_id / "llm" / call_id
        self._write_diagnostic(
            diagnostic_dir / "request.json",
            {
                "stage": stage.value,
                "purpose": purpose.value,
                "profile": resolved.profile_name,
                "flavor": resolved.settings.flavor.value,
                "reasoning": resolved.settings.reasoning.value,
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


def _require_collection_snapshot(snapshot: SourceSnapshot) -> CollectionSourceSnapshot:
    if snapshot.collection is None:
        raise ScopeError("the source snapshot is not collection-scoped")
    return snapshot.collection


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


def _available_collection_sources(collection: CollectionSourceSnapshot) -> set[ContextSource]:
    sources = {ContextSource.CONVERSATION, ContextSource.PREVIOUS_QA}
    if collection.synthesis is not None:
        sources.add(ContextSource.COLLECTION_SUMMARY)
    kinds = {artifact.kind for paper in collection.papers for artifact in paper.artifacts}
    if "summary_json" in kinds:
        sources.add(ContextSource.PAPER_SUMMARIES)
    if "extracted_json" in kinds:
        sources.add(ContextSource.RAW)
    return sources


def _paper_scoring_text(
    paper: PaperSourceSnapshot,
    summary: StructuredSummary | None,
    synthesis_text: str | None,
) -> str:
    """Compact per-paper document for lexical selection (bounded for CPU)."""

    parts = [paper.title or ""]
    if summary is not None:
        parts.append(summary.model_dump_json()[:20_000])
    elif synthesis_text is not None:
        parts.append(synthesis_text[:20_000])
    return " ".join(part for part in parts if part)


def _rewrite_schema() -> str:
    return json.dumps(RewriteResult.model_json_schema(), ensure_ascii=False, indent=2)


def _answer_schema() -> str:
    return json.dumps(StructuredAnswer.model_json_schema(), ensure_ascii=False, indent=2)


def _rank_qa_candidates(question: str, candidates: list[QaRecord]) -> list[QaRecord]:
    terms = set(re.findall(r"[\w]+", normalize_question(question), flags=re.UNICODE))

    def score(candidate: QaRecord) -> tuple[int, str, str]:
        candidate_terms = set(re.findall(r"[\w]+", candidate.normalized_question, flags=re.UNICODE))
        return (len(terms & candidate_terms), candidate.created_at, candidate.id)

    return sorted(candidates, key=score, reverse=True)


def _conversation_title(candidate: str | None, conversation: Conversation) -> str:
    if candidate is not None:
        title = " ".join(candidate.split()).strip("\"'")
        if title:
            return title[:80].rstrip()
    return conversation.created_at[:16]


def _snapshot_changes(previous: SourceSnapshot, current: SourceSnapshot) -> list[str]:
    if source_fingerprint(previous) == source_fingerprint(current):
        return []
    if previous.scope != current.scope:
        return ["scope_changed"]
    reasons: list[str] = []
    if previous.paper is not None and current.paper is not None:
        reasons.extend(_paper_snapshot_changes(previous.paper, current.paper, prefix=""))
    elif previous.collection is not None and current.collection is not None:
        reasons.extend(_collection_snapshot_changes(previous.collection, current.collection))
    else:
        return ["scope_changed"]
    for field in (
        "context_builder_version",
        "retrieval_version",
        "prompt_version",
        "answer_schema_version",
    ):
        if getattr(previous, field) != getattr(current, field):
            reasons.append(f"{field}_changed")
    return reasons or ["source_fingerprint_changed"]


def _paper_snapshot_changes(
    previous: PaperSourceSnapshot, current: PaperSourceSnapshot, *, prefix: str
) -> list[str]:
    reasons: list[str] = []
    if previous.status != current.status:
        reasons.append(f"{prefix}paper_status_changed")
    previous_artifacts = {artifact.kind: artifact for artifact in previous.artifacts}
    current_artifacts = {artifact.kind: artifact for artifact in current.artifacts}
    for kind in sorted(previous_artifacts.keys() | current_artifacts.keys()):
        old = previous_artifacts.get(kind)
        new = current_artifacts.get(kind)
        if old is None or new is None:
            reasons.append(f"{prefix}{kind}_availability_changed")
        elif old.sha256 != new.sha256:
            reasons.append(f"{prefix}{kind}_content_changed")
        elif old.schema_version != new.schema_version:
            reasons.append(f"{prefix}{kind}_schema_changed")
        elif old.artifact_id != new.artifact_id:
            reasons.append(f"{prefix}{kind}_artifact_changed")
    return reasons


def _collection_snapshot_changes(
    previous: CollectionSourceSnapshot, current: CollectionSourceSnapshot
) -> list[str]:
    reasons: list[str] = []
    old_ids = [paper.paper_id for paper in previous.papers]
    new_ids = [paper.paper_id for paper in current.papers]
    if set(old_ids) != set(new_ids):
        reasons.append("collection_members_changed")
    elif old_ids != new_ids:
        reasons.append("collection_order_changed")
    if previous.name != current.name or previous.description != current.description:
        reasons.append("collection_metadata_changed")
    current_by_id = {paper.paper_id: paper for paper in current.papers}
    for paper in previous.papers:
        current_paper = current_by_id.get(paper.paper_id)
        if current_paper is None:
            continue
        reasons.extend(_paper_snapshot_changes(paper, current_paper, prefix=f"{paper.paper_id}:"))
    old_synthesis = previous.synthesis
    new_synthesis = current.synthesis
    if (old_synthesis is None) != (new_synthesis is None):
        reasons.append("synthesis_availability_changed")
    elif old_synthesis is not None and new_synthesis is not None:
        if old_synthesis.sha256 != new_synthesis.sha256:
            reasons.append("synthesis_content_changed")
        elif old_synthesis.artifact_id != new_synthesis.artifact_id:
            reasons.append("synthesis_artifact_changed")
    return reasons
