from pathlib import Path

import pytest

from passagen.storage import ArtifactRow, PaperRow, session_scope
from passagen.storage.database import connect_database, initialize_database


def test_artifact_and_status_write_roll_back_together(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(RuntimeError, match="abort"), session_scope(database_path) as session:
        session.add(PaperRow(id="paper-1", original_filename="paper.pdf", pdf_sha256="a" * 64))
        session.add(
            ArtifactRow(
                id="artifact-1",
                paper_id="paper-1",
                kind="extracted_json",
                path="papers/paper-1/extracted.json",
            )
        )
        raise RuntimeError("abort")

    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
