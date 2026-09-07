from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "paper_id",
            sa.Text(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "collection_id",
            sa.Text(),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("(paper_id IS NULL) <> (collection_id IS NULL)"),
    )
    op.create_index("ix_conversations_paper_id", "conversations", ["paper_id"])
    op.create_index("ix_conversations_collection_id", "conversations", ["collection_id"])

    op.create_table(
        "generation_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "paper_id",
            sa.Text(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "collection_id",
            sa.Text(),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Text(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "qa_record_id",
            sa.Text(),
            sa.ForeignKey("qa_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('answer', 'collection_synthesis', 'report')"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'interrupted')"),
    )
    op.create_index("ix_generation_runs_conversation_id", "generation_runs", ["conversation_id"])
    op.create_index("ix_generation_runs_collection_id", "generation_runs", ["collection_id"])
    op.create_index("ix_generation_runs_status", "generation_runs", ["status"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Text(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("generation_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')"),
        sa.CheckConstraint("status IN ('pending', 'completed', 'failed')"),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )

    op.create_table(
        "qa_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Text(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_message_id",
            sa.Text(),
            sa.ForeignKey("conversation_messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "answer_message_id",
            sa.Text(),
            sa.ForeignKey("conversation_messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("standalone_question", sa.Text(), nullable=False),
        sa.Column("normalized_question", sa.Text(), nullable=False),
        sa.Column("normalized_question_hash", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("context_plan_json", sa.Text(), nullable=False),
        sa.Column("answer_json", sa.Text(), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("answer_schema_version", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.Text(), nullable=True),
        sa.Column("archive_title", sa.Text(), nullable=True),
        sa.Column("archive_tags_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )
    op.create_index("ix_qa_records_conversation_id", "qa_records", ["conversation_id"])
    op.create_index(
        "ix_qa_records_normalized_question_hash", "qa_records", ["normalized_question_hash"]
    )
    op.create_index("ix_qa_records_archived_at", "qa_records", ["archived_at"])

    op.create_table(
        "qa_citations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "qa_record_id",
            sa.Text(),
            sa.ForeignKey("qa_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "paper_id",
            sa.Text(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("summary_path", sa.Text(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.CheckConstraint("page_start IS NULL OR page_end IS NULL OR page_end >= page_start"),
    )
    op.create_index("ix_qa_citations_qa_record_id", "qa_citations", ["qa_record_id"])
    op.create_index("ix_qa_citations_paper_id", "qa_citations", ["paper_id"])

    op.create_table(
        "generation_llm_calls",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "generation_run_id",
            sa.Text(),
            sa.ForeignKey("generation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "stage IN ('rewrite', 'route', 'retrieve', 'map', 'reduce', 'answer', 'repair')"
        ),
    )
    op.create_index(
        "ix_generation_llm_calls_generation_run_id",
        "generation_llm_calls",
        ["generation_run_id"],
    )


def downgrade() -> None:
    op.drop_table("generation_llm_calls")
    op.drop_table("qa_citations")
    op.drop_table("qa_records")
    op.drop_table("conversation_messages")
    op.drop_table("generation_runs")
    op.drop_table("conversations")
