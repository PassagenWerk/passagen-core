from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from passagen.assistant import repository
from passagen.assistant.schemas import (
    ContextPlan,
    ContextSource,
    MessageRole,
    MessageStatus,
    QaRecord,
    QuestionIntent,
    SourceSnapshot,
    StructuredAnswer,
    source_fingerprint,
)
from passagen.storage.database import connect_database, initialize_database

SHA = "a" * 64


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "passagen.db"
    initialize_database(path)
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO papers (id, original_filename, pdf_sha256) VALUES ('paper-1', 'a.pdf', ?)",
            (SHA,),
        )
    return path


def _snapshot() -> SourceSnapshot:
    return SourceSnapshot.model_validate(
        {
            "scope": "paper",
            "paper": {"paper_id": "paper-1", "status": "summarized", "artifacts": []},
            "context_builder_version": "1",
            "retrieval_version": "1",
            "prompt_version": "1",
            "answer_schema_version": "1",
        }
    )


def _record(
    conversation_id: str,
    question_message_id: str,
    answer_message_id: str,
) -> QaRecord:
    snapshot = _snapshot()
    return QaRecord(
        id="qa-1",
        conversation_id=conversation_id,
        question_message_id=question_message_id,
        answer_message_id=answer_message_id,
        standalone_question="What workload was used?",
        normalized_question="what workload was used?",
        normalized_question_hash="b" * 64,
        intent=QuestionIntent.FACT_LOOKUP,
        context_plan=ContextPlan(
            standalone_question="What workload was used?",
            intent=QuestionIntent.FACT_LOOKUP,
            sources=[ContextSource.SUMMARY],
        ),
        answer=StructuredAnswer(
            standalone_question="What workload was used?",
            intent=QuestionIntent.FACT_LOOKUP,
            answer_markdown="The workload was W.",
        ),
        source_snapshot=snapshot,
        source_fingerprint=source_fingerprint(snapshot),
        prompt_version="1",
        answer_schema_version="1",
        created_at="2026-09-07 10:00:00",
    )


def test_conversation_crud(database_path: Path) -> None:
    conversation = repository.create_conversation(
        database_path, paper_id="paper-1", title="Latency"
    )

    assert repository.get_conversation(database_path, conversation.id) == conversation
    assert [c.title for c in repository.list_conversations(database_path, paper_id="paper-1")] == [
        "Latency"
    ]

    renamed = repository.rename_conversation(database_path, conversation.id, "Scheduling")
    assert renamed.title == "Scheduling"

    assert repository.delete_conversation(database_path, conversation.id) is True
    assert repository.get_conversation(database_path, conversation.id) is None
    assert repository.delete_conversation(database_path, conversation.id) is False


def test_create_conversation_requires_an_existing_paper(database_path: Path) -> None:
    with pytest.raises(repository.ConversationNotFoundError):
        repository.create_conversation(database_path, paper_id="missing", title="x")


def test_messages_keep_insertion_order(database_path: Path) -> None:
    conversation = repository.create_conversation(database_path, paper_id="paper-1", title="t")
    first = repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.USER,
        content="q1",
        status=MessageStatus.COMPLETED,
    )
    second = repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.ASSISTANT,
        content="a1",
        status=MessageStatus.COMPLETED,
    )

    messages = repository.list_messages(database_path, conversation.id)

    assert [m.id for m in messages] == [first.id, second.id]


def test_save_qa_turn_persists_record_and_completes_run(database_path: Path) -> None:
    conversation = repository.create_conversation(database_path, paper_id="paper-1", title="t")
    run_id = repository.create_generation_run(
        database_path, kind="answer", paper_id="paper-1", conversation_id=conversation.id
    )
    question = repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.USER,
        content="q",
        status=MessageStatus.COMPLETED,
    )
    answer = repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.PENDING,
        run_id=run_id,
    )
    record = _record(conversation.id, question.id, answer.id)

    repository.save_qa_turn(
        database_path, record=record, answer_content="The workload was W.", run_id=run_id
    )

    stored = repository.get_qa_record(database_path, record.id)
    assert stored is not None
    assert stored.answer.answer_markdown == "The workload was W."
    assert [r.id for r in repository.list_qa_records(database_path, conversation.id)] == [record.id]
    run = repository.get_generation_run(database_path, run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.qa_record_id == record.id
    messages = {m.id: m for m in repository.list_messages(database_path, conversation.id)}
    assert messages[answer.id].status is MessageStatus.COMPLETED
    assert messages[answer.id].content == "The workload was W."


def test_fail_turn_marks_message_and_run(database_path: Path) -> None:
    conversation = repository.create_conversation(database_path, paper_id="paper-1", title="t")
    run_id = repository.create_generation_run(database_path, kind="answer")
    answer = repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.PENDING,
        run_id=run_id,
    )

    repository.fail_turn(
        database_path,
        answer_message_id=answer.id,
        run_id=run_id,
        error_code="provider_error",
        error_message="timeout",
    )

    run = repository.get_generation_run(database_path, run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "provider_error"
    message = repository.list_messages(database_path, conversation.id)[0]
    assert message.status is MessageStatus.FAILED


def test_interrupt_active_generation_runs(database_path: Path) -> None:
    running = repository.create_generation_run(database_path, kind="answer")

    assert repository.interrupt_active_generation_runs(database_path) == 1
    assert repository.interrupt_active_generation_runs(database_path) == 0

    run = repository.get_generation_run(database_path, running)
    assert run is not None
    assert run.status == "interrupted"


def test_record_generation_llm_call(database_path: Path) -> None:
    run_id = repository.create_generation_run(database_path, kind="answer")

    repository.record_generation_llm_call(
        database_path,
        run_id,
        call_id="call-1",
        stage="answer",
        provider="fake",
        model="fake-model",
        prompt_version="1",
        schema_version="1",
        input_tokens=10,
        output_tokens=20,
        finish_reason="stop",
    )

    with connect_database(database_path) as connection:
        row = connection.execute(
            "SELECT stage, output_tokens FROM generation_llm_calls WHERE id = 'call-1'"
        ).fetchone()
    assert row is not None
    assert tuple(row) == ("answer", 20)


def test_deleting_conversation_cascades_records(database_path: Path) -> None:
    conversation = repository.create_conversation(database_path, paper_id="paper-1", title="t")
    repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.USER,
        content="q",
        status=MessageStatus.COMPLETED,
    )

    repository.delete_conversation(database_path, conversation.id)

    with connect_database(database_path) as connection:
        count = connection.execute("SELECT count(*) FROM conversation_messages").fetchone()[0]
    assert count == 0


def test_messages_reference_run(database_path: Path) -> None:
    conversation = repository.create_conversation(database_path, paper_id="paper-1", title="t")
    run_id = repository.create_generation_run(database_path, kind="answer")
    message = repository.add_message(
        database_path,
        conversation.id,
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.PENDING,
        run_id=run_id,
    )

    assert message.run_id == run_id
    with (
        pytest.raises(sqlite3.IntegrityError),
        connect_database(database_path) as connection,
    ):
        connection.execute(
            """
            INSERT INTO conversation_messages
                (id, conversation_id, role, content, status, run_id)
            VALUES ('m-9', ?, 'assistant', '', 'pending', 'missing-run')
            """,
            (conversation.id,),
        )
