import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None

_ARTIFACT_KINDS = (
    "synthesis_json",
    "synthesis_markdown",
    "synthesis_source",
    "report_json",
    "report_markdown",
    "report_source",
    "report_input",
)


def upgrade() -> None:
    # SQLite cannot alter a CHECK constraint; rebuild collection_artifacts with
    # the extended kind list while preserving every indexed artifact.
    op.create_table(
        "collection_artifacts_new",
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
        sa.CheckConstraint(f"kind IN ({', '.join(repr(kind) for kind in _ARTIFACT_KINDS)})"),
        sa.UniqueConstraint("generation_run_id", "kind"),
    )
    op.execute(
        """
        INSERT INTO collection_artifacts_new (
            id, collection_id, generation_run_id, kind, path, version, sha256,
            size_bytes, source_fingerprint, created_at
        )
        SELECT id, collection_id, generation_run_id, kind, path, version, sha256,
               size_bytes, source_fingerprint, created_at
        FROM collection_artifacts
        """
    )
    op.drop_table("collection_artifacts")
    op.rename_table("collection_artifacts_new", "collection_artifacts")
    op.create_index(
        "ix_collection_artifacts_collection_id", "collection_artifacts", ["collection_id"]
    )
    op.create_index(
        "ix_collection_artifacts_fingerprint",
        "collection_artifacts",
        ["collection_id", "source_fingerprint"],
    )

    op.create_table(
        "collection_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "collection_id",
            sa.Text(),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("user_prompt", sa.Text()),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("generation_runs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "report_artifact_id",
            sa.Text(),
            sa.ForeignKey("collection_artifacts.id", ondelete="SET NULL"),
        ),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.Text()),
        sa.CheckConstraint("kind IN ('review', 'comparison', 'gaps', 'custom')"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"),
    )
    op.create_index("ix_collection_reports_collection_id", "collection_reports", ["collection_id"])
    op.create_index("ix_collection_reports_run_id", "collection_reports", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_collection_reports_run_id", table_name="collection_reports")
    op.drop_index("ix_collection_reports_collection_id", table_name="collection_reports")
    op.drop_table("collection_reports")

    op.create_table(
        "collection_artifacts_new",
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
    op.execute(
        """
        INSERT INTO collection_artifacts_new (
            id, collection_id, generation_run_id, kind, path, version, sha256,
            size_bytes, source_fingerprint, created_at
        )
        SELECT id, collection_id, generation_run_id, kind, path, version, sha256,
               size_bytes, source_fingerprint, created_at
        FROM collection_artifacts
        WHERE kind IN ('synthesis_json', 'synthesis_markdown', 'synthesis_source')
        """
    )
    op.drop_table("collection_artifacts")
    op.rename_table("collection_artifacts_new", "collection_artifacts")
    op.create_index(
        "ix_collection_artifacts_collection_id", "collection_artifacts", ["collection_id"]
    )
    op.create_index(
        "ix_collection_artifacts_fingerprint",
        "collection_artifacts",
        ["collection_id", "source_fingerprint"],
    )
