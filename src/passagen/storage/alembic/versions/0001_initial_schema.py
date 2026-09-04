"""Create the initial Passagen schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text()),
        sa.Column("authors_json", sa.Text()),
        sa.Column("year", sa.Integer()),
        sa.Column("venue", sa.Text()),
        sa.Column("doi", sa.Text()),
        sa.Column("arxiv_id", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("metadata_sources_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("pdf_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="discovered"),
        sa.Column(
            "created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint(
            "status IN ('discovered', 'parsed', 'metadata_resolved', 'summarized', 'outlined')"
        ),
    )
    op.create_index("ux_papers_doi", "papers", ["doi"], unique=True)
    op.create_index("ux_papers_arxiv_id", "papers", ["arxiv_id"], unique=True)
    op.create_index("ux_papers_pdf_sha256", "papers", ["pdf_sha256"], unique=True)
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "paper_id", sa.Text(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("version", sa.Text()),
        sa.Column("sha256", sa.Text()),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column(
            "created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index("ix_artifacts_paper_id", "artifacts", ["paper_id"])
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "paper_id", sa.Text(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "started_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("finished_at", sa.Text()),
    )
    op.create_index("ix_processing_runs_paper_id", "processing_runs", ["paper_id"])
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "processing_run_id",
            sa.Text(),
            sa.ForeignKey("processing_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index("ix_llm_calls_processing_run_id", "llm_calls", ["processing_run_id"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("processing_runs")
    op.drop_table("artifacts")
    op.drop_table("papers")
