"""Persistence for conversations, messages, QA records, and generation runs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from passagen.assistant.schemas import (
    ContextPlan,
    Conversation,
    ConversationScope,
    Message,
    MessageRole,
    MessageStatus,
    QaRecord,
    QuestionIntent,
    SourceSnapshot,
    StructuredAnswer,
)
from passagen.storage.engine import session_scope
from passagen.storage.models import (
    ConversationMessageRow,
    ConversationRow,
    GenerationLlmCallRow,
    GenerationRunRow,
    PaperRow,
    QaCitationRow,
    QaRecordRow,
)


class ConversationNotFoundError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationRunRecord:
    id: str
    kind: str
    status: str
    paper_id: str | None
    collection_id: str | None
    conversation_id: str | None
    qa_record_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


def create_conversation(database_path: Path, *, paper_id: str, title: str) -> Conversation:
    conversation_id = str(uuid.uuid4())
    with session_scope(database_path) as session:
        if session.get(PaperRow, paper_id) is None:
            raise ConversationNotFoundError(f"Paper not found: {paper_id}")
        session.add(
            ConversationRow(id=conversation_id, paper_id=paper_id, collection_id=None, title=title)
        )
        session.flush()
        row = session.get(ConversationRow, conversation_id)
        if row is None:
            raise RuntimeError(f"Failed to reload conversation {conversation_id}")
        return _conversation(row)


def list_conversations(database_path: Path, *, paper_id: str) -> tuple[Conversation, ...]:
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(ConversationRow)
            .where(ConversationRow.paper_id == paper_id)
            .order_by(ConversationRow.updated_at.desc(), ConversationRow.id)
        ).all()
        return tuple(_conversation(row) for row in rows)


def get_conversation(database_path: Path, conversation_id: str) -> Conversation | None:
    with session_scope(database_path) as session:
        row = session.get(ConversationRow, conversation_id)
        return _conversation(row) if row is not None else None


def rename_conversation(database_path: Path, conversation_id: str, title: str) -> Conversation:
    with session_scope(database_path) as session:
        row = session.get(ConversationRow, conversation_id)
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        row.title = title
        row.updated_at = _now(session)
        session.flush()
        return _conversation(row)


def delete_conversation(database_path: Path, conversation_id: str) -> bool:
    with session_scope(database_path) as session:
        row = session.get(ConversationRow, conversation_id)
        if row is None:
            return False
        session.delete(row)
        return True


def list_messages(database_path: Path, conversation_id: str) -> tuple[Message, ...]:
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(ConversationMessageRow)
            .where(ConversationMessageRow.conversation_id == conversation_id)
            .order_by(text("rowid"))
        ).all()
        return tuple(_message(row) for row in rows)


def add_message(
    database_path: Path,
    conversation_id: str,
    *,
    role: MessageRole,
    content: str,
    status: MessageStatus,
    run_id: str | None = None,
) -> Message:
    message_id = str(uuid.uuid4())
    with session_scope(database_path) as session:
        session.add(
            ConversationMessageRow(
                id=message_id,
                conversation_id=conversation_id,
                role=role.value,
                content=content,
                status=status.value,
                run_id=run_id,
            )
        )
        session.flush()
        row = session.get(ConversationMessageRow, message_id)
        if row is None:
            raise RuntimeError(f"Failed to reload message {message_id}")
        return _message(row)


def create_generation_run(
    database_path: Path,
    *,
    kind: str,
    paper_id: str | None = None,
    collection_id: str | None = None,
    conversation_id: str | None = None,
    source_snapshot_json: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    with session_scope(database_path) as session:
        session.add(
            GenerationRunRow(
                id=run_id,
                kind=kind,
                paper_id=paper_id,
                collection_id=collection_id,
                conversation_id=conversation_id,
                status="running",
                source_snapshot_json=source_snapshot_json,
                started_at=_now(session),
            )
        )
    return run_id


def get_generation_run(database_path: Path, run_id: str) -> GenerationRunRecord | None:
    with session_scope(database_path) as session:
        row = session.get(GenerationRunRow, run_id)
        return _generation_run(row) if row is not None else None


def fail_turn(
    database_path: Path,
    *,
    answer_message_id: str,
    run_id: str,
    error_code: str,
    error_message: str,
) -> None:
    with session_scope(database_path) as session:
        message = session.get(ConversationMessageRow, answer_message_id)
        if message is not None:
            message.status = MessageStatus.FAILED.value
        run = session.get(GenerationRunRow, run_id)
        if run is not None:
            run.status = "failed"
            run.error_code = error_code
            run.error_message = error_message
            run.completed_at = _now(session)


def save_qa_turn(
    database_path: Path,
    *,
    record: QaRecord,
    answer_content: str,
    run_id: str,
) -> None:
    """Atomically persist a completed answer message, QA record, citations, and run."""

    with session_scope(database_path) as session:
        message = session.get(ConversationMessageRow, record.answer_message_id)
        if message is None:
            raise ConversationNotFoundError(record.answer_message_id)
        message.status = MessageStatus.COMPLETED.value
        message.content = answer_content
        session.add(
            QaRecordRow(
                id=record.id,
                conversation_id=record.conversation_id,
                question_message_id=record.question_message_id,
                answer_message_id=record.answer_message_id,
                standalone_question=record.standalone_question,
                normalized_question=record.normalized_question,
                normalized_question_hash=record.normalized_question_hash,
                intent=record.intent.value,
                context_plan_json=record.context_plan.model_dump_json(),
                answer_json=record.answer.model_dump_json(),
                source_snapshot_json=record.source_snapshot.model_dump_json(),
                source_fingerprint=record.source_fingerprint,
                prompt_version=record.prompt_version,
                answer_schema_version=record.answer_schema_version,
            )
        )
        for citation in record.answer.citations:
            session.add(
                QaCitationRow(
                    id=str(uuid.uuid4()),
                    qa_record_id=record.id,
                    paper_id=citation.paper_id,
                    artifact_kind=citation.artifact_kind.value,
                    artifact_id=citation.artifact_id,
                    artifact_sha256=citation.artifact_sha256,
                    summary_path=citation.summary_path,
                    section=citation.section,
                    page_start=citation.page_start,
                    page_end=citation.page_end,
                    excerpt=citation.excerpt,
                )
            )
        run = session.get(GenerationRunRow, run_id)
        if run is None:
            raise ConversationNotFoundError(f"Generation run not found: {run_id}")
        run.status = "completed"
        run.qa_record_id = record.id
        run.completed_at = _now(session)
        conversation = session.get(ConversationRow, record.conversation_id)
        if conversation is not None:
            conversation.updated_at = _now(session)


def record_generation_llm_call(
    database_path: Path,
    run_id: str,
    *,
    call_id: str,
    stage: str,
    provider: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    input_tokens: int | None,
    output_tokens: int | None,
    finish_reason: str | None,
    error_message: str | None = None,
) -> None:
    with session_scope(database_path) as session:
        session.add(
            GenerationLlmCallRow(
                id=call_id,
                generation_run_id=run_id,
                stage=stage,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                finish_reason=finish_reason,
                error_message=error_message,
            )
        )


def get_qa_record(database_path: Path, qa_record_id: str) -> QaRecord | None:
    with session_scope(database_path) as session:
        row = session.get(QaRecordRow, qa_record_id)
        return _qa_record(row) if row is not None else None


def list_qa_records(database_path: Path, conversation_id: str) -> tuple[QaRecord, ...]:
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(QaRecordRow)
            .where(QaRecordRow.conversation_id == conversation_id)
            .order_by(text("rowid"))
        ).all()
        return tuple(_qa_record(row) for row in rows)


def interrupt_active_generation_runs(database_path: Path) -> int:
    """Mark queued/running runs as interrupted after a process restart."""

    with session_scope(database_path) as session:
        result = session.execute(
            update(GenerationRunRow)
            .where(GenerationRunRow.status.in_(("queued", "running")))
            .values(status="interrupted", completed_at=func.current_timestamp())
        )
        if not isinstance(result, CursorResult):
            return 0
        return int(result.rowcount or 0)


def _now(session: Session) -> str:
    return str(session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))))


def _conversation(row: ConversationRow) -> Conversation:
    scope = ConversationScope.PAPER if row.paper_id is not None else ConversationScope.COLLECTION
    return Conversation(
        id=row.id,
        scope=scope,
        paper_id=row.paper_id,
        collection_id=row.collection_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message(row: ConversationMessageRow) -> Message:
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        role=MessageRole(row.role),
        content=row.content,
        status=MessageStatus(row.status),
        run_id=row.run_id,
        created_at=row.created_at,
    )


def _qa_record(row: QaRecordRow) -> QaRecord:
    return QaRecord(
        id=row.id,
        conversation_id=row.conversation_id,
        question_message_id=row.question_message_id,
        answer_message_id=row.answer_message_id,
        standalone_question=row.standalone_question,
        normalized_question=row.normalized_question,
        normalized_question_hash=row.normalized_question_hash,
        intent=QuestionIntent(row.intent),
        context_plan=ContextPlan.model_validate_json(row.context_plan_json),
        answer=StructuredAnswer.model_validate_json(row.answer_json),
        source_snapshot=SourceSnapshot.model_validate_json(row.source_snapshot_json),
        source_fingerprint=row.source_fingerprint,
        prompt_version=row.prompt_version,
        answer_schema_version=row.answer_schema_version,
        archived_at=row.archived_at,
        archive_title=row.archive_title,
        archive_tags=[str(tag) for tag in json.loads(row.archive_tags_json or "[]")],
        created_at=row.created_at,
    )


def _generation_run(row: GenerationRunRow) -> GenerationRunRecord:
    return GenerationRunRecord(
        id=row.id,
        kind=row.kind,
        status=row.status,
        paper_id=row.paper_id,
        collection_id=row.collection_id,
        conversation_id=row.conversation_id,
        qa_record_id=row.qa_record_id,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )
