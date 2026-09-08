import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "collection_artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "collection_id",
            sa.Text(),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "generation_run_id",
            sa.Text(),
            sa.ForeignKey("generation_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("kind IN ('synthesis_json', 'synthesis_markdown', 'synthesis_source')"),
        sa.UniqueConstraint("generation_run_id", "kind"),
    )
    op.create_index(
        "ix_collection_artifacts_collection_id", "collection_artifacts", ["collection_id"]
    )
    op.create_index(
        "ix_collection_artifacts_fingerprint",
        "collection_artifacts",
        ["collection_id", "source_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_artifacts_fingerprint", table_name="collection_artifacts")
    op.drop_index("ix_collection_artifacts_collection_id", table_name="collection_artifacts")
    op.drop_table("collection_artifacts")
