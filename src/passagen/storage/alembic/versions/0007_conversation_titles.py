from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE conversations SET title = substr(created_at, 1, 16) "
            "WHERE title = 'New conversation'"
        )
    )


def downgrade() -> None:
    # The previous default titles cannot be distinguished from user-entered timestamps.
    pass
