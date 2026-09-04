"""Add persisted batch update runs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "update_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("paper_ids_json", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("from_stage", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("current_paper_id", sa.Text()),
        sa.Column("current_stage", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("result_json", sa.Text()),
        sa.Column(
            "created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("finished_at", sa.Text()),
        sa.CheckConstraint("mode IN ('continue', 'rebuild')", name="ck_update_runs_mode"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'interrupted')",
            name="ck_update_runs_status",
        ),
    )
    op.create_index("ix_update_runs_status", "update_runs", ["status"])
    op.create_index("ix_update_runs_created_at", "update_runs", ["created_at"])


def downgrade() -> None:
    op.drop_table("update_runs")
