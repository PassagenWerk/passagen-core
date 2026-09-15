import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "paper_citations",
        sa.Column(
            "paper_id",
            sa.Text(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("format", sa.Text(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citation_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("authoritative", sa.Integer(), nullable=False),
        sa.Column("source_identifier", sa.Text()),
        sa.Column("metadata_fingerprint", sa.Text(), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column("remote_status", sa.Text(), nullable=False),
        sa.Column("remote_checked_at", sa.Text()),
        sa.Column("next_remote_attempt_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("format IN ('bibtex')"),
        sa.CheckConstraint("source IN ('doi', 'arxiv', 'local_metadata')"),
        sa.CheckConstraint("authoritative IN (0, 1)"),
        sa.CheckConstraint("remote_status IN ('success', 'not_found', 'failed', 'not_attempted')"),
        sa.CheckConstraint("length(content) <= 1000000"),
    )
    op.create_index(
        "ix_paper_citations_source_identifier",
        "paper_citations",
        ["source_identifier"],
    )
    op.create_index(
        "ux_paper_citations_local_key",
        "paper_citations",
        ["format", "citation_key"],
        unique=True,
        sqlite_where=sa.text("source IN ('arxiv', 'local_metadata')"),
    )


def downgrade() -> None:
    op.drop_index("ux_paper_citations_local_key", table_name="paper_citations")
    op.drop_index("ix_paper_citations_source_identifier", table_name="paper_citations")
    op.drop_table("paper_citations")
