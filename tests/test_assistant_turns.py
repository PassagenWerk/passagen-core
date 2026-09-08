from __future__ import annotations

from pathlib import Path

import pytest
from support import FakeProvider, assistant_env, assistant_service, scripted_responder

from passagen.assistant import repository
from passagen.assistant.errors import AssistantNotFoundError, ScopeError, StaleSourceError
from passagen.assistant.schemas import MessageStatus
from passagen.storage.database import connect_database


def test_submit_turn_queues_and_execute_turn_completes(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1", title="Latency").id

    submission = service.submit_turn(conversation_id, "这篇论文要解决什么问题，主要贡献是什么？")

    assert submission.answer_message.status is MessageStatus.PENDING
    run = repository.get_generation_run(env.database_path, submission.run_id)
    assert run is not None and run.status == "queued"
    claimed = service.claim_next_queued_run()
    assert claimed is not None and claimed.id == submission.run_id
    assert service.claim_next_queued_run() is None

    turn = service.execute_turn(submission.run_id)

    assert turn.qa_record.source_snapshot.scope.value == "paper"
    assert turn.answer_message.status is MessageStatus.COMPLETED


def test_queued_run_rejects_changed_submission_snapshot(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1", title="Latency").id
    submission = service.submit_turn(conversation_id, "具体数值是多少？")

    with connect_database(env.database_path) as connection:
        connection.execute("UPDATE artifacts SET sha256 = ? WHERE id = 'art-summary'", ("0" * 64,))

    with pytest.raises(StaleSourceError):
        service.execute_turn(submission.run_id)

    run = service.get_generation_run(submission.run_id)
    assert run.status == "failed"
    assert run.error_code == "stale_source"


def test_archive_search_and_export_qa_record(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1", title="Latency").id
    turn = service.ask(conversation_id, "这篇论文要解决什么问题，主要贡献是什么？")
    record = turn.qa_record

    archived = service.archive_qa_record(
        record.id, title=" 延迟 workload ", tags=["eval", " latency "]
    )

    assert archived.archived_at is not None
    assert archived.archive_title == "延迟 workload"
    assert archived.archive_tags == ["eval", "latency"]
    stored = service.get_qa_record(record.id)
    assert stored.archived_at is not None

    hits = service.search_qa_records("workload", archived=True, paper_id="paper-1")
    assert [hit.id for hit in hits] == [record.id]
    assert [hit.id for hit in service.search_qa_records("eval", archived=True)] == [record.id]
    assert service.search_qa_records("workload", archived=False) == ()
    assert service.search_qa_records("不存在的词", archived=True) == ()

    exported = service.get_qa_record(record.id).model_dump_json()
    assert '"archive_title":"延迟 workload"' in exported

    unarchived = service.unarchive_qa_record(record.id)
    assert unarchived.archived_at is None
    assert unarchived.archive_title is None


def test_archive_requires_existing_record_and_title(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))

    with pytest.raises(AssistantNotFoundError):
        service.archive_qa_record("missing-record", title="t")
    with pytest.raises(ScopeError, match="blank"):
        service.archive_qa_record("missing-record", title="  ")
    with pytest.raises(AssistantNotFoundError):
        service.get_qa_record("missing-record")


def test_search_escapes_like_wildcards(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1", title="Latency").id
    service.ask(conversation_id, "这篇论文要解决什么问题，主要贡献是什么？")

    assert service.search_qa_records("100%") == ()
    assert service.search_qa_records("什么", archived=None)


def test_queued_turn_history_excludes_later_questions(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1", title="Latency").id
    first = service.submit_turn(conversation_id, "第一个问题")
    service.submit_turn(conversation_id, "不应出现在历史中的第二个问题")

    service.execute_turn(first.run_id)

    rewrite_prompt = provider.prompts[0]
    assert "第一个问题" in rewrite_prompt
    assert "不应出现在历史中的第二个问题" not in rewrite_prompt


def test_interrupt_active_run_also_fails_pending_message(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))
    conversation_id = service.create_conversation("paper-1", title="Latency").id
    submission = service.submit_turn(conversation_id, "问题？")

    assert service.interrupt_active_runs() == 1

    run = service.get_generation_run(submission.run_id)
    message = service.get_conversation(conversation_id).messages[-1]
    assert run.status == "interrupted"
    assert run.error_code == "interrupted"
    assert message.status is MessageStatus.FAILED
