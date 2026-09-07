"""Persisted assistant contracts: conversations, QA records, snapshots, and plans.

These Pydantic models are the versioned boundary between Core services and the
CLI/Web adapters. Everything stored in ``qa_records.answer_json``,
``qa_records.context_plan_json``, or ``source_snapshot_json`` must round-trip
through these models.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from passagen.assistant.versions import (
    ANSWER_SCHEMA_VERSION,
    CONTEXT_PLAN_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)


def _require_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("must not be blank")
    return text


NonBlankStr = Annotated[str, AfterValidator(_require_text)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ConversationScope(StrEnum):
    PAPER = "paper"
    COLLECTION = "collection"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class GenerationRunKind(StrEnum):
    ANSWER = "answer"
    COLLECTION_SYNTHESIS = "collection_synthesis"
    REPORT = "report"


class GenerationRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class GenerationStage(StrEnum):
    REWRITE = "rewrite"
    ROUTE = "route"
    RETRIEVE = "retrieve"
    MAP = "map"
    REDUCE = "reduce"
    ANSWER = "answer"
    REPAIR = "repair"


class QuestionIntent(StrEnum):
    OVERVIEW = "overview"
    STRUCTURE = "structure"
    FACT_LOOKUP = "fact_lookup"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    SYNTHESIS = "synthesis"
    OTHER = "other"


class ContextSource(StrEnum):
    CONVERSATION = "conversation"
    PREVIOUS_QA = "previous_qa"
    SUMMARY = "summary"
    OUTLINE = "outline"
    RAW = "raw"
    COLLECTION_SUMMARY = "collection_summary"
    PAPER_SUMMARIES = "paper_summaries"


class AnswerKind(StrEnum):
    DIRECT = "direct"
    COMPARATIVE = "comparative"
    SYNTHESIS = "synthesis"


class CitationArtifactKind(StrEnum):
    EXTRACTED = "extracted_json"
    SUMMARY = "summary_json"
    OUTLINE = "outline_md"


class ArtifactRef(BaseModel):
    """Immutable pointer to one artifact as used by a source snapshot."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: NonBlankStr
    kind: NonBlankStr
    schema_version: NonBlankStr
    sha256: Sha256Hex


class PaperSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: NonBlankStr
    title: str | None = None
    status: NonBlankStr
    artifacts: list[ArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _artifact_kinds_are_unique(self) -> Self:
        kinds = [artifact.kind for artifact in self.artifacts]
        if len(kinds) != len(set(kinds)):
            raise ValueError("artifact kinds must be unique within a paper snapshot")
        return self


class CollectionSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_id: NonBlankStr
    name: NonBlankStr
    description: str | None = None
    papers: list[PaperSourceSnapshot] = Field(min_length=1)
    synthesis: ArtifactRef | None = None

    @model_validator(mode="after")
    def _papers_are_unique(self) -> Self:
        paper_ids = [paper.paper_id for paper in self.papers]
        if len(paper_ids) != len(set(paper_ids)):
            raise ValueError("collection snapshot papers must be unique")
        return self


class SourceSnapshot(BaseModel):
    """The exact, immutable source set one answer or report was generated from."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = SNAPSHOT_SCHEMA_VERSION
    scope: ConversationScope
    paper: PaperSourceSnapshot | None = None
    collection: CollectionSourceSnapshot | None = None
    context_builder_version: NonBlankStr
    retrieval_version: NonBlankStr
    prompt_version: NonBlankStr
    answer_schema_version: NonBlankStr

    @model_validator(mode="after")
    def _scope_matches_payload(self) -> Self:
        if self.scope is ConversationScope.PAPER:
            if self.paper is None or self.collection is not None:
                raise ValueError("paper scope requires exactly the paper snapshot")
        elif self.paper is not None or self.collection is None:
            raise ValueError("collection scope requires exactly the collection snapshot")
        return self

    def paper_ids(self) -> tuple[str, ...]:
        if self.paper is not None:
            return (self.paper.paper_id,)
        if self.collection is not None:
            return tuple(paper.paper_id for paper in self.collection.papers)
        return ()


def source_fingerprint(snapshot: SourceSnapshot) -> str:
    """Content hash of a snapshot; changes whenever any used source changes."""

    canonical = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ContextPlan(BaseModel):
    """Validated planner output describing which sources an answer may use."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = CONTEXT_PLAN_VERSION
    standalone_question: NonBlankStr
    intent: QuestionIntent
    reuse_qa_id: NonBlankStr | None = None
    sources: list[ContextSource] = Field(min_length=1)
    paper_ids: list[NonBlankStr] = Field(default_factory=list)
    retrieval_queries: list[NonBlankStr] = Field(default_factory=list)
    requires_exact_quote: bool = False
    answer_kind: AnswerKind = AnswerKind.DIRECT

    @model_validator(mode="after")
    def _sources_and_queries_are_consistent(self) -> Self:
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("context sources must be unique")
        if len(self.paper_ids) != len(set(self.paper_ids)):
            raise ValueError("plan paper_ids must be unique")
        if ContextSource.RAW in self.sources and not self.retrieval_queries:
            raise ValueError("raw context requires at least one retrieval query")
        return self


class Citation(BaseModel):
    """A verifiable pointer from one claim to content inside the source snapshot."""

    model_config = ConfigDict(extra="forbid")

    citation_id: NonBlankStr
    paper_id: NonBlankStr
    artifact_kind: CitationArtifactKind
    artifact_id: NonBlankStr | None = None
    artifact_sha256: Sha256Hex
    summary_path: NonBlankStr | None = None
    section: NonBlankStr | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    excerpt: NonBlankStr | None = None

    @model_validator(mode="after")
    def _locators_are_consistent(self) -> Self:
        if self.summary_path is None and self.section is None and self.page_start is None:
            raise ValueError("a citation requires a summary path, section, or page locator")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must not precede page_start")
        if self.page_end is not None and self.page_start is None:
            raise ValueError("page_end requires page_start")
        return self


class AnswerClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: NonBlankStr
    citation_ids: list[NonBlankStr] = Field(min_length=1)


class StructuredAnswer(BaseModel):
    """Versioned answer contract persisted in ``qa_records.answer_json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = ANSWER_SCHEMA_VERSION
    standalone_question: NonBlankStr
    intent: QuestionIntent
    answer_markdown: NonBlankStr
    claims: list[AnswerClaim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    limitations: list[NonBlankStr] = Field(default_factory=list)
    follow_up_questions: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _claims_resolve_to_citations(self) -> Self:
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("citation ids must be unique within an answer")
        known = set(citation_ids)
        for claim in self.claims:
            missing = [
                citation_id for citation_id in claim.citation_ids if citation_id not in known
            ]
            if missing:
                raise ValueError(f"claim references unknown citations: {', '.join(missing)}")
        return self


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    scope: ConversationScope
    paper_id: NonBlankStr | None = None
    collection_id: NonBlankStr | None = None
    title: str
    created_at: NonBlankStr
    updated_at: NonBlankStr

    @model_validator(mode="after")
    def _scope_matches_target(self) -> Self:
        if self.scope is ConversationScope.PAPER:
            if self.paper_id is None or self.collection_id is not None:
                raise ValueError("paper scope requires exactly paper_id")
        elif self.paper_id is not None or self.collection_id is None:
            raise ValueError("collection scope requires exactly collection_id")
        return self


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    conversation_id: NonBlankStr
    role: MessageRole
    content: str
    status: MessageStatus
    run_id: NonBlankStr | None = None
    created_at: NonBlankStr


class QaRecord(BaseModel):
    """One complete, retrievable, and reusable question-answer turn."""

    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    conversation_id: NonBlankStr
    question_message_id: NonBlankStr
    answer_message_id: NonBlankStr
    standalone_question: NonBlankStr
    normalized_question: str
    normalized_question_hash: Sha256Hex
    intent: QuestionIntent
    context_plan: ContextPlan
    answer: StructuredAnswer
    source_snapshot: SourceSnapshot
    source_fingerprint: Sha256Hex
    prompt_version: NonBlankStr
    answer_schema_version: NonBlankStr
    archived_at: NonBlankStr | None = None
    archive_title: NonBlankStr | None = None
    archive_tags: list[NonBlankStr] = Field(default_factory=list)
    created_at: NonBlankStr

    @model_validator(mode="after")
    def _record_is_internally_consistent(self) -> Self:
        if self.source_fingerprint != source_fingerprint(self.source_snapshot):
            raise ValueError("source_fingerprint does not match the source snapshot")
        if self.answer.standalone_question != self.standalone_question:
            raise ValueError("answer standalone question must match the record question")
        snapshot_papers = set(self.source_snapshot.paper_ids())
        for citation in self.answer.citations:
            if citation.paper_id not in snapshot_papers:
                raise ValueError(f"citation paper {citation.paper_id} is outside the snapshot")
        return self
