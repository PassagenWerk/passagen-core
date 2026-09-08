from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PaperRow(Base):
    __tablename__ = "papers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('discovered', 'parsed', 'metadata_resolved', 'summarized', 'outlined')"
        ),
        Index("ux_papers_doi", "doi", unique=True),
        Index("ux_papers_arxiv_id", "arxiv_id", unique=True),
        Index("ux_papers_pdf_sha256", "pdf_sha256", unique=True),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    authors_json: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    venue: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(Text)
    arxiv_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    metadata_sources_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'{}'")
    )
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'discovered'"))
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    artifacts: Mapped[list[ArtifactRow]] = relationship(
        back_populates="paper", passive_deletes=True
    )
    processing_runs: Mapped[list[ProcessingRunRow]] = relationship(
        back_populates="paper", passive_deletes=True
    )
    tag_assignments: Mapped[list[PaperTagRow]] = relationship(passive_deletes=True)
    collection_memberships: Mapped[list[CollectionPaperRow]] = relationship(passive_deletes=True)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_paper_id", "paper_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    paper: Mapped[PaperRow] = relationship(back_populates="artifacts")


class PaperSectionRow(Base):
    __tablename__ = "paper_sections"
    __table_args__ = (
        UniqueConstraint("paper_id", "ordinal"),
        CheckConstraint("ordinal >= 0"),
        Index("ix_paper_sections_paper_id", "paper_id"),
        Index("ix_paper_sections_artifact", "paper_id", "extracted_artifact_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    pages_json: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_artifact_id: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)


class ProcessingRunRow(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (Index("ix_processing_runs_paper_id", "paper_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    finished_at: Mapped[str | None] = mapped_column(Text)

    paper: Mapped[PaperRow] = relationship(back_populates="processing_runs")
    llm_calls: Mapped[list[LlmCallRow]] = relationship(
        back_populates="processing_run", passive_deletes=True
    )


class UpdateRunRow(Base):
    __tablename__ = "update_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('continue', 'rebuild')"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'interrupted')"),
        Index("ix_update_runs_status", "status"),
        Index("ix_update_runs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    from_stage: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    current_paper_id: Mapped[str | None] = mapped_column(Text)
    current_stage: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    finished_at: Mapped[str | None] = mapped_column(Text)


class LlmCallRow(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_processing_run_id", "processing_run_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    processing_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    processing_run: Mapped[ProcessingRunRow] = relationship(back_populates="llm_calls")


class TagRow(Base):
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class PaperTagRow(Base):
    __tablename__ = "paper_tags"
    __table_args__ = (UniqueConstraint("paper_id", "tag_id"),)

    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class CollectionRow(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class CollectionPaperRow(Base):
    __tablename__ = "collection_papers"
    __table_args__ = (
        UniqueConstraint("collection_id", "paper_id"),
        UniqueConstraint("collection_id", "position"),
        CheckConstraint("position >= 0"),
    )

    collection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("(paper_id IS NULL) <> (collection_id IS NULL)"),
        Index("ix_conversations_paper_id", "paper_id"),
        Index("ix_conversations_collection_id", "collection_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str | None] = mapped_column(Text, ForeignKey("papers.id", ondelete="CASCADE"))
    collection_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("collections.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    messages: Mapped[list[ConversationMessageRow]] = relationship(
        back_populates="conversation", passive_deletes=True
    )
    qa_records: Mapped[list[QaRecordRow]] = relationship(
        back_populates="conversation", passive_deletes=True
    )


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')"),
        CheckConstraint("status IN ('pending', 'completed', 'failed')"),
        Index("ix_conversation_messages_conversation_id", "conversation_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("generation_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    conversation: Mapped[ConversationRow] = relationship(back_populates="messages")


class QaRecordRow(Base):
    __tablename__ = "qa_records"
    __table_args__ = (
        Index("ix_qa_records_conversation_id", "conversation_id"),
        Index("ix_qa_records_normalized_question_hash", "normalized_question_hash"),
        Index("ix_qa_records_archived_at", "archived_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    question_message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False
    )
    answer_message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False
    )
    standalone_question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question_hash: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    context_plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    answer_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    answer_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[str | None] = mapped_column(Text)
    archive_title: Mapped[str | None] = mapped_column(Text)
    archive_tags_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    conversation: Mapped[ConversationRow] = relationship(back_populates="qa_records")
    citations: Mapped[list[QaCitationRow]] = relationship(
        back_populates="qa_record", passive_deletes=True
    )


class QaCitationRow(Base):
    __tablename__ = "qa_citations"
    __table_args__ = (
        CheckConstraint("page_start IS NULL OR page_end IS NULL OR page_end >= page_start"),
        Index("ix_qa_citations_qa_record_id", "qa_record_id"),
        Index("ix_qa_citations_paper_id", "paper_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    qa_record_id: Mapped[str] = mapped_column(
        Text, ForeignKey("qa_records.id", ondelete="CASCADE"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        Text, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    summary_path: Mapped[str | None] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    excerpt: Mapped[str | None] = mapped_column(Text)

    qa_record: Mapped[QaRecordRow] = relationship(back_populates="citations")


class GenerationRunRow(Base):
    __tablename__ = "generation_runs"
    __table_args__ = (
        CheckConstraint("kind IN ('answer', 'collection_synthesis', 'report')"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'interrupted')"),
        Index("ix_generation_runs_conversation_id", "conversation_id"),
        Index("ix_generation_runs_collection_id", "collection_id"),
        Index("ix_generation_runs_status", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    paper_id: Mapped[str | None] = mapped_column(Text, ForeignKey("papers.id", ondelete="CASCADE"))
    collection_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("collections.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    qa_record_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("qa_records.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    reuse_policy: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'auto'"))
    source_snapshot_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    started_at: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)

    llm_calls: Mapped[list[GenerationLlmCallRow]] = relationship(
        back_populates="generation_run", passive_deletes=True
    )


class GenerationLlmCallRow(Base):
    __tablename__ = "generation_llm_calls"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('rewrite', 'route', 'retrieve', 'map', 'reduce', 'answer', 'repair')"
        ),
        Index("ix_generation_llm_calls_generation_run_id", "generation_run_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    generation_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    generation_run: Mapped[GenerationRunRow] = relationship(back_populates="llm_calls")


class CollectionArtifactRow(Base):
    __tablename__ = "collection_artifacts"
    __table_args__ = (
        CheckConstraint("kind IN ('synthesis_json', 'synthesis_markdown', 'synthesis_source')"),
        UniqueConstraint("generation_run_id", "kind"),
        Index("ix_collection_artifacts_collection_id", "collection_id"),
        Index("ix_collection_artifacts_fingerprint", "collection_id", "source_fingerprint"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    collection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    generation_run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("generation_runs.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
