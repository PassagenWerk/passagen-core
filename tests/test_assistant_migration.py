from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from passagen.storage.database import connect_database, initialize_database
from passagen.storage.migrations import alembic_revision, head_revision

ASSISTANT_TABLES = {
    "conversations",
    "conversation_messages",
    "qa_records",
    "qa_citations",
    "generation_runs",
    "generation_llm_calls",
    "paper_sections",
    "paper_sections_fts",
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _alembic_config(database_path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "src" / "passagen" / "storage" / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _insert_paper(connection: sqlite3.Connection, paper_id: str = "paper-1") -> None:
    connection.execute(
        "INSERT INTO papers (id, original_filename, pdf_sha256) VALUES (?, ?, ?)",
        (paper_id, f"{paper_id}.pdf", paper_id.replace("paper", "a").ljust(64, "b")),
    )


def _insert_conversation(
    connection: sqlite3.Connection, conversation_id: str = "conversation-1"
) -> None:
    connection.execute(
        "INSERT INTO conversations (id, paper_id, title) VALUES (?, 'paper-1', ?)",
        (conversation_id, "Questions"),
    )


def _insert_message(
    connection: sqlite3.Connection,
    message_id: str,
    role: str,
    *,
    conversation_id: str = "conversation-1",
) -> None:
    connection.execute(
        """
        INSERT INTO conversation_messages (id, conversation_id, role, content, status)
        VALUES (?, ?, ?, 'content', 'completed')
        """,
        (message_id, conversation_id, role),
    )


def _insert_qa_record(connection: sqlite3.Connection, record_id: str = "qa-1") -> None:
    connection.execute(
        """
        INSERT INTO qa_records (
            id, conversation_id, question_message_id, answer_message_id,
            standalone_question, normalized_question, normalized_question_hash,
            intent, context_plan_json, answer_json, source_snapshot_json,
            source_fingerprint, prompt_version, answer_schema_version
        ) VALUES (?, 'conversation-1', 'message-q', 'message-a', 'q', 'q', ?, 'overview',
                  '{}', '{}', '{}', ?, '1', '1')
        """,
        (record_id, "c" * 64, "d" * 64),
    )


def _seed_qa_chain(connection: sqlite3.Connection) -> None:
    _insert_paper(connection)
    _insert_conversation(connection)
    _insert_message(connection, "message-q", "user")
    _insert_message(connection, "message-a", "assistant")
    _insert_qa_record(connection)
    connection.execute(
        """
        INSERT INTO qa_citations (id, qa_record_id, paper_id, artifact_kind, artifact_sha256,
                                  summary_path)
        VALUES ('citation-1', 'qa-1', 'paper-1', 'summary_json', ?, 'evaluation.results[0]')
        """,
        ("e" * 64,),
    )


def test_initialize_creates_assistant_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"

    initialize_database(database_path)

    with connect_database(database_path) as connection:
        assert _tables(connection) >= ASSISTANT_TABLES


def test_existing_default_conversation_titles_are_backfilled(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0006")
    with connect_database(database_path) as connection:
        _insert_paper(connection)
        connection.execute(
            """
            INSERT INTO conversations (id, paper_id, title, created_at)
            VALUES ('conversation-1', 'paper-1', 'New conversation', '2026-09-08 14:32:01')
            """
        )

    command.upgrade(config, "head")

    with connect_database(database_path) as connection:
        title = connection.execute(
            "SELECT title FROM conversations WHERE id = 'conversation-1'"
        ).fetchone()[0]
    assert title == "2026-09-08 14:32"


def test_conversation_requires_exactly_one_scope_target(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute(
            "INSERT INTO conversations (id, title) VALUES ('conversation-1', 'no scope')"
        )

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        _insert_paper(connection)
        connection.execute("INSERT INTO collections (id, name) VALUES ('collection-1', 'c')")
        connection.execute(
            """
            INSERT INTO conversations (id, paper_id, collection_id, title)
            VALUES ('conversation-1', 'paper-1', 'collection-1', 'both')
            """
        )


def test_message_role_is_checked(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        _insert_paper(connection)
        _insert_conversation(connection)
        connection.execute(
            """
            INSERT INTO conversation_messages (id, conversation_id, role, content, status)
            VALUES ('message-1', 'conversation-1', 'system', 'content', 'pending')
            """
        )


def test_message_status_is_checked(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        _insert_paper(connection)
        _insert_conversation(connection)
        connection.execute(
            """
            INSERT INTO conversation_messages (id, conversation_id, role, content, status)
            VALUES ('message-1', 'conversation-1', 'user', 'content', 'streaming')
            """
        )


def test_generation_run_kind_and_status_are_checked(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute(
            "INSERT INTO generation_runs (id, kind, status) VALUES ('run-1', 'chat', 'queued')"
        )
    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute(
            "INSERT INTO generation_runs (id, kind, status) VALUES ('run-1', 'answer', 'stuck')"
        )


def test_generation_llm_call_stage_is_checked(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute("INSERT INTO generation_runs (id, kind) VALUES ('run-1', 'answer')")
        connection.execute(
            """
            INSERT INTO generation_llm_calls
                (id, generation_run_id, stage, provider, model, prompt_version, schema_version)
            VALUES ('call-1', 'run-1', 'guess', 'fake', 'fake-model', '1', '1')
            """
        )


def test_citation_page_range_is_checked(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        _seed_qa_chain(connection)
        connection.execute(
            """
            UPDATE qa_citations SET page_start = 6, page_end = 5 WHERE id = 'citation-1'
            """
        )


def test_deleting_paper_cascades_the_conversation_chain(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        _seed_qa_chain(connection)
        connection.execute(
            """
            INSERT INTO generation_runs (id, kind, conversation_id, qa_record_id)
            VALUES ('run-1', 'answer', 'conversation-1', 'qa-1')
            """
        )
        connection.execute(
            """
            INSERT INTO generation_llm_calls
                (id, generation_run_id, stage, provider, model, prompt_version, schema_version)
            VALUES ('call-1', 'run-1', 'answer', 'fake', 'fake-model', '1', '1')
            """
        )
        connection.execute("DELETE FROM papers WHERE id = 'paper-1'")

        remaining = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in sorted(ASSISTANT_TABLES)
        }
    assert remaining == dict.fromkeys(sorted(ASSISTANT_TABLES), 0)


def test_deleting_conversation_cascades_messages_records_and_citations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        _seed_qa_chain(connection)
        connection.execute("DELETE FROM conversations WHERE id = 'conversation-1'")

        assert connection.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM qa_records").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM qa_citations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM papers").fetchone()[0] == 1


def test_qa_record_message_ids_are_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        _seed_qa_chain(connection)
        connection.execute(
            """
            INSERT INTO qa_records (
                id, conversation_id, question_message_id, answer_message_id,
                standalone_question, normalized_question, normalized_question_hash,
                intent, context_plan_json, answer_json, source_snapshot_json,
                source_fingerprint, prompt_version, answer_schema_version
            ) VALUES ('qa-2', 'conversation-1', 'message-q', 'message-a', 'q2', 'q2', ?, 'overview',
                      '{}', '{}', '{}', ?, '1', '1')
            """,
            ("f" * 64, "0" * 64),
        )


def test_run_reference_survives_qa_record_deletion(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        _seed_qa_chain(connection)
        connection.execute(
            """
            INSERT INTO generation_runs (id, kind, qa_record_id)
            VALUES ('run-1', 'answer', 'qa-1')
            """
        )

        connection.execute("DELETE FROM qa_records WHERE id = 'qa-1'")

        row = connection.execute(
            "SELECT qa_record_id FROM generation_runs WHERE id = 'run-1'"
        ).fetchone()
    assert row is not None
    assert row["qa_record_id"] is None


def test_message_run_reference_survives_run_deletion(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        _insert_paper(connection)
        _insert_conversation(connection)
        connection.execute("INSERT INTO generation_runs (id, kind) VALUES ('run-1', 'answer')")
        connection.execute(
            """
            INSERT INTO conversation_messages (id, conversation_id, role, content, status, run_id)
            VALUES ('message-1', 'conversation-1', 'user', 'q', 'failed', 'run-1')
            """
        )

        connection.execute("DELETE FROM generation_runs WHERE id = 'run-1'")

        row = connection.execute(
            "SELECT run_id FROM conversation_messages WHERE id = 'message-1'"
        ).fetchone()
    assert row is not None
    assert row["run_id"] is None


def test_messages_require_an_existing_conversation(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO conversation_messages (id, conversation_id, role, content, status)
            VALUES ('message-1', 'missing-conversation', 'user', 'q', 'pending')
            """
        )


def test_migration_downgrade_and_reupgrade_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    assert alembic_revision(database_path) == head_revision()

    command.downgrade(_alembic_config(database_path), "0005")

    assert alembic_revision(database_path) == "0005"
    with connect_database(database_path) as connection:
        assert not (ASSISTANT_TABLES & _tables(connection))

    command.upgrade(_alembic_config(database_path), "head")

    assert alembic_revision(database_path) == head_revision()
    with connect_database(database_path) as connection:
        assert _tables(connection) >= ASSISTANT_TABLES
        issues = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert issues == []
