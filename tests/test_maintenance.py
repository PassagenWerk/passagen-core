from __future__ import annotations

import hashlib
from pathlib import Path

from passagen.domain import Paper
from passagen.storage.database import backup_database, connect_database, initialize_database
from passagen.storage.maintenance import check_artifacts
from passagen.storage.repository import register_pdf


def test_database_backup_is_a_consistent_sqlite_copy(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "passagen.db"
    initialize_database(database_path)
    paper = Paper(original_filename="paper.pdf", pdf_sha256="a" * 64, file_size_bytes=1)
    register_pdf(database_path, paper, Path("pdfs/aa/paper.pdf"))

    backup_path = backup_database(database_path, tmp_path / "backups" / "passagen.db")

    with connect_database(backup_path) as connection:
        assert connection.execute("SELECT id FROM papers").fetchone()[0] == paper.id


def test_artifact_check_validates_file_size_and_hash(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    initialize_database(database_path)
    content = b"%PDF-1.7\ncontent\n%%EOF\n"
    digest = hashlib.sha256(content).hexdigest()
    paper = Paper(original_filename="paper.pdf", pdf_sha256=digest, file_size_bytes=len(content))
    path = Path("pdfs") / digest[:2] / f"{digest}.pdf"
    target = data_dir / path
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    register_pdf(database_path, paper, path)

    valid = check_artifacts(database_path, data_dir)
    target.write_bytes(b"corrupt")
    corrupt = check_artifacts(database_path, data_dir)

    assert valid.checked == 1
    assert valid.issues == []
    assert len(corrupt.issues) == 1
    assert "size mismatch" in corrupt.issues[0].message
