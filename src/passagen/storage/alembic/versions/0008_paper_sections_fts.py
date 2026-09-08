from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_runs",
        sa.Column("reuse_policy", sa.Text(), nullable=False, server_default="auto"),
    )
    op.create_table(
        "paper_sections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "paper_id",
            sa.Text(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("pages_json", sa.Text(), nullable=False),
        sa.Column("extracted_artifact_id", sa.Text(), nullable=False),
        sa.Column("extracted_artifact_sha256", sa.Text(), nullable=False),
        sa.UniqueConstraint("paper_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 0"),
    )
    op.create_index("ix_paper_sections_paper_id", "paper_sections", ["paper_id"])
    op.create_index(
        "ix_paper_sections_artifact",
        "paper_sections",
        ["paper_id", "extracted_artifact_sha256"],
    )
    op.execute(
        sa.text(
            "CREATE VIRTUAL TABLE paper_sections_fts USING fts5("
            "title, text, content='paper_sections', content_rowid='id')"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER paper_sections_ai AFTER INSERT ON paper_sections BEGIN "
            "INSERT INTO paper_sections_fts(rowid, title, text) "
            "VALUES (new.id, new.title, new.text); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER paper_sections_ad AFTER DELETE ON paper_sections BEGIN "
            "INSERT INTO paper_sections_fts(paper_sections_fts, rowid, title, text) "
            "VALUES ('delete', old.id, old.title, old.text); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER paper_sections_au AFTER UPDATE ON paper_sections BEGIN "
            "INSERT INTO paper_sections_fts(paper_sections_fts, rowid, title, text) "
            "VALUES ('delete', old.id, old.title, old.text); "
            "INSERT INTO paper_sections_fts(rowid, title, text) "
            "VALUES (new.id, new.title, new.text); END"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS paper_sections_au"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS paper_sections_ad"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS paper_sections_ai"))
    op.execute(sa.text("DROP TABLE IF EXISTS paper_sections_fts"))
    op.drop_index("ix_paper_sections_artifact", table_name="paper_sections")
    op.drop_index("ix_paper_sections_paper_id", table_name="paper_sections")
    op.drop_table("paper_sections")
    op.drop_column("generation_runs", "reuse_policy")
