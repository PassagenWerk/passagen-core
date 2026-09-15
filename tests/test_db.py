from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from passagen.storage.database import (
    SCHEMA_VERSION,
    DatabaseVersionError,
    connect_database,
    current_version,
    initialize_database,
)
from passagen.storage.migrations import alembic_revision, head_revision


def insert_paper(connection: sqlite3.Connection, paper_id: str, sha256: str) -> None:
    connection.execute(
        """
        INSERT INTO papers (id, original_filename, pdf_sha256)
        VALUES (?, ?, ?)
        """,
        (paper_id, "paper.pdf", sha256),
    )


def test_initialize_database_creates_current_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "passagen.db"

    initialize_database(database_path)
    initialize_database(database_path)

    assert current_version(database_path) == SCHEMA_VERSION
    with connect_database(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        paper_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(papers)").fetchall()
        }
        artifact_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
        }
    assert {
        "papers",
        "artifacts",
        "processing_runs",
        "llm_calls",
        "tags",
        "paper_tags",
        "collections",
        "collection_papers",
        "paper_citations",
    } <= tables
    assert "metadata_sources_json" in paper_columns
    assert "abstract" in paper_columns
    assert "note" in paper_columns
    assert "size_bytes" in artifact_columns
    assert alembic_revision(database_path) == head_revision()


def test_initialize_database_stamps_existing_v1_without_losing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        insert_paper(connection, "paper-1", "a" * 64)
        connection.execute("DROP TABLE alembic_version")

    initialize_database(database_path)

    with connect_database(database_path) as connection:
        row = connection.execute("SELECT id, pdf_sha256 FROM papers").fetchone()
    assert row is not None
    assert tuple(row) == ("paper-1", "a" * 64)
    assert current_version(database_path) == SCHEMA_VERSION
    assert alembic_revision(database_path) == head_revision()


def test_initialize_database_migrates_schema_10_to_persisted_citations(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        insert_paper(connection, "paper-1", "a" * 64)
        connection.execute("DROP TABLE paper_citations")
        connection.execute("UPDATE alembic_version SET version_num = '0010'")
        connection.execute("PRAGMA user_version = 10")

    initialize_database(database_path)

    with connect_database(database_path) as connection:
        paper = connection.execute("SELECT id FROM papers WHERE id = 'paper-1'").fetchone()
        citation_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_citations'"
        ).fetchone()
    assert paper is not None
    assert citation_table is not None
    assert current_version(database_path) == 11
    assert alembic_revision(database_path) == "0011"


def test_initialize_database_rejects_unversioned_existing_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.execute("PRAGMA user_version = 0")

    with pytest.raises(DatabaseVersionError, match="incomplete or unsupported"):
        initialize_database(database_path)


def test_initialize_database_rejects_schema_11_without_citation_table(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        connection.execute("DROP TABLE paper_citations")
        connection.execute("DROP TABLE alembic_version")

    with pytest.raises(DatabaseVersionError, match="paper_citations"):
        initialize_database(database_path)


def test_sha256_is_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        insert_paper(connection, "paper-1", "a" * 64)
        insert_paper(connection, "paper-2", "a" * 64)


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_removed_paper_statuses_are_rejected(tmp_path: Path, status: str) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute(
            "INSERT INTO papers (id, original_filename, pdf_sha256, status) VALUES (?, ?, ?, ?)",
            ("paper-1", "paper.pdf", "a" * 64, status),
        )


def test_managed_pdf_is_recorded_as_relative_artifact_path(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    sha256 = "a" * 64
    managed_path = f"pdfs/{sha256[:2]}/{sha256}.pdf"
    with connect_database(database_path) as connection:
        insert_paper(connection, "paper-1", sha256)
        connection.execute(
            """
            INSERT INTO artifacts (id, paper_id, kind, path, sha256)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("pdf-1", "paper-1", "original_pdf", managed_path, sha256),
        )

        row = connection.execute("SELECT path FROM artifacts WHERE id = ?", ("pdf-1",)).fetchone()

    assert row is not None
    assert row["path"] == managed_path


def test_foreign_keys_are_enabled(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute(
            "INSERT INTO artifacts (id, paper_id, kind, path) VALUES (?, ?, ?, ?)",
            ("artifact-1", "missing-paper", "pdf", "/paper.pdf"),
        )


def test_paper_deletion_cascades_to_persisted_citations(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        insert_paper(connection, "paper-1", "a" * 64)
        connection.execute(
            """
            INSERT INTO paper_citations (
                paper_id, format, content, citation_key, source, authoritative,
                metadata_fingerprint, generator_version, remote_status
            ) VALUES (?, 'bibtex', ?, ?, 'local_metadata', 0, ?, 'bibtex-v2', 'not_attempted')
            """,
            ("paper-1", "@misc{paper-1,}\n", "paper-1", "fingerprint"),
        )
        connection.execute("DELETE FROM papers WHERE id = ?", ("paper-1",))
        citation = connection.execute(
            "SELECT paper_id FROM paper_citations WHERE paper_id = ?", ("paper-1",)
        ).fetchone()

    assert citation is None


def test_rejects_newer_database_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    with connect_database(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(DatabaseVersionError):
        initialize_database(database_path)
