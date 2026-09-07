from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import (
    FakeProvider,
    assistant_env,
    assistant_service,
    good_answer,
    outline_citation,
    raw_citation,
    scripted_responder,
)

from passagen.assistant import repository
from passagen.assistant.errors import (
    AnswerValidationError,
    AssistantNotFoundError,
    ProviderCallError,
    ScopeError,
)
from passagen.assistant.schemas import ContextSource, MessageStatus, QuestionIntent
from passagen.assistant.service import ConversationService
from passagen.storage.database import connect_database


def _create(service: ConversationService) -> str:
    return service.create_conversation("paper-1", title="Latency").id


def test_overview_question_uses_summary_only(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = _create(service)

    turn = service.ask(conversation_id, "这篇论文要解决什么问题，主要贡献是什么？")

    assert turn.qa_record.context_plan.sources == [ContextSource.SUMMARY]
    assert turn.qa_record.intent is QuestionIntent.OVERVIEW
    answer_prompt = provider.prompts[-1]
    assert "[source summary_json" in answer_prompt
    assert "[source extracted_json" not in answer_prompt
    assert turn.answer_message.status is MessageStatus.COMPLETED
    run = repository.get_generation_run(env.database_path, turn.run_id)
    assert run is not None and run.status == "completed"
    stored = repository.get_qa_record(env.database_path, turn.qa_record.id)
    assert stored == turn.qa_record
    with connect_database(env.database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute(
                "SELECT stage FROM generation_llm_calls ORDER BY rowid"
            ).fetchall()
        ]
    assert stages == ["rewrite", "answer"]


def test_structure_question_uses_outline(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env, citation=outline_citation(env)))
    service = assistant_service(env, provider)

    turn = service.ask(_create(service), "文章是怎么组织的？评估部分在哪一节？")

    assert turn.qa_record.context_plan.sources == [ContextSource.OUTLINE]
    assert "[source outline_md" in provider.prompts[-1]


def test_fact_question_adds_raw_sections_without_full_text(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env, citation=raw_citation(env)))
    service = assistant_service(env, provider)

    turn = service.ask(
        conversation_id := _create(service), "延迟实验使用的是什么 workload，具体数值是多少？"
    )

    assert turn.qa_record.context_plan.sources == [ContextSource.SUMMARY, ContextSource.RAW]
    answer_prompt = provider.prompts[-1]
    assert "Latency dropped to 12 ms." in answer_prompt
    assert "Prior schedulers are surveyed here." not in answer_prompt
    with connect_database(env.database_path) as connection:
        citations = connection.execute(
            "SELECT artifact_kind, section, page_start FROM qa_citations"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in citations] == [
        ("extracted_json", "4.2 Latency Evaluation", 5)
    ]
    detail = service.get_conversation(conversation_id)
    assert [m.role.value for m in detail.messages] == ["user", "assistant"]


def test_follow_up_is_rewritten_and_uses_history(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(
        scripted_responder(env, rewrite_question="What was the second experiment?")
    )
    service = assistant_service(env, provider)
    conversation_id = _create(service)
    service.ask(conversation_id, "这篇论文做了哪些实验？")

    turn = service.ask(conversation_id, "那它的第二个实验呢？")

    assert turn.qa_record.standalone_question == "What was the second experiment?"
    assert turn.qa_record.context_plan.sources[0] is ContextSource.CONVERSATION
    rewrite_prompt = provider.prompts[-2]
    assert "这篇论文做了哪些实验？" in rewrite_prompt


def test_provider_failure_keeps_question_and_allows_retry(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    failing = FakeProvider(scripted_responder(env, fail_rewrite=True))
    service = assistant_service(env, failing)
    conversation_id = _create(service)

    with pytest.raises(ProviderCallError):
        service.ask(conversation_id, "这篇论文要解决什么问题？")

    detail = service.get_conversation(conversation_id)
    assert [m.status for m in detail.messages] == [MessageStatus.COMPLETED, MessageStatus.FAILED]
    assert detail.messages[0].content == "这篇论文要解决什么问题？"
    assert repository.list_qa_records(env.database_path, conversation_id) == ()
    with connect_database(env.database_path) as connection:
        run = connection.execute("SELECT status, error_code FROM generation_runs").fetchone()
    assert tuple(run) == ("failed", "provider_error")

    working = FakeProvider(scripted_responder(env))
    retry_service = assistant_service(env, working)
    turn = retry_service.ask(conversation_id, "这篇论文要解决什么问题，主要贡献是什么？")
    assert turn.qa_record.id
    assert len(service.get_conversation(conversation_id).messages) == 4


def test_invalid_citation_is_repaired(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    bad_answer = good_answer(env, "具体数值是多少？")
    bad_answer = bad_answer.replace(env.summary_sha, "0" * 64)
    provider = FakeProvider(scripted_responder(env, first_answer=bad_answer))
    service = assistant_service(env, provider)

    turn = service.ask(_create(service), "具体数值是多少？")

    assert turn.answer_message.status is MessageStatus.COMPLETED
    with connect_database(env.database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute(
                "SELECT stage FROM generation_llm_calls ORDER BY rowid"
            ).fetchall()
        ]
    assert stages == ["rewrite", "answer", "repair"]


def test_unrepairable_citation_fails_turn_atomically(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    question = "具体数值是多少？"
    bad_answer = good_answer(env, question).replace(env.summary_sha, "0" * 64)
    provider = FakeProvider(
        scripted_responder(env, first_answer=bad_answer, repair_answer=bad_answer)
    )
    service = assistant_service(env, provider)
    conversation_id = _create(service)

    with pytest.raises(AnswerValidationError):
        service.ask(conversation_id, "具体数值是多少？")

    assert repository.list_qa_records(env.database_path, conversation_id) == ()
    detail = service.get_conversation(conversation_id)
    assert detail.messages[-1].status is MessageStatus.FAILED
    with connect_database(env.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM qa_citations").fetchone()[0] == 0
        run = connection.execute("SELECT status, error_code FROM generation_runs").fetchone()
    assert tuple(run) == ("failed", "invalid_answer")


def test_locator_less_citation_is_repaired(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    question = "什么是 eBPF？"
    locator_less = json.dumps(
        {
            "standalone_question": "What is eBPF?",
            "intent": "fact_lookup",
            "answer_markdown": "eBPF is a standard for kernel programmability [c-1].",
            "claims": [{"text": "eBPF is a standard.", "citation_ids": ["c-1"]}],
            "citations": [
                {
                    "citation_id": "c-1",
                    "paper_id": "paper-1",
                    "artifact_kind": "summary_json",
                    "artifact_id": env.summary_artifact_id,
                    "artifact_sha256": env.summary_sha,
                    "summary_path": None,
                    "section": None,
                    "page_start": None,
                    "excerpt": "eBPF is a standard for kernel programmability.",
                }
            ],
            "limitations": [],
            "follow_up_questions": [],
        }
    )
    provider = FakeProvider(scripted_responder(env, first_answer=locator_less))
    service = assistant_service(env, provider)

    turn = service.ask(_create(service), question)

    assert turn.answer_message.status is MessageStatus.COMPLETED
    assert turn.qa_record.answer.standalone_question == question
    assert turn.qa_record.answer.intent is turn.qa_record.context_plan.intent
    repair_prompt = provider.prompts[-1]
    assert "<sources>" in repair_prompt
    assert '"evaluation"' in repair_prompt
    assert '"results"' in repair_prompt
    with connect_database(env.database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute(
                "SELECT stage FROM generation_llm_calls ORDER BY rowid"
            ).fetchall()
        ]
    assert stages == ["rewrite", "answer", "repair"]


def test_answer_diagnostics_are_written_to_run_directory(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)

    turn = service.ask(_create(service), "这篇论文要解决什么问题，主要贡献是什么？")

    call_dirs = sorted((env.data_dir / "runs" / turn.run_id / "llm").iterdir())
    assert len(call_dirs) == 2
    for call_dir in call_dirs:
        assert (call_dir / "request.json").is_file()
        assert (call_dir / "response.json").is_file()


def test_ask_requires_processed_paper(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    with connect_database(env.database_path) as connection:
        connection.execute(
            "INSERT INTO papers (id, original_filename, pdf_sha256) VALUES ('paper-2', 'b.pdf', ?)",
            ("e" * 64,),
        )
    service = assistant_service(env, FakeProvider(scripted_responder(env)))
    conversation_id = service.create_conversation("paper-2").id

    with pytest.raises(ScopeError, match="extracted_json"):
        service.ask(conversation_id, "问题？")


def test_ask_rejects_blank_question_and_missing_conversation(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))

    with pytest.raises(ScopeError, match="blank"):
        service.ask(_create(service), "   ")
    with pytest.raises(AssistantNotFoundError, match="not found"):
        service.ask("missing-conversation", "问题？")


def test_conversation_management(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))
    conversation = service.create_conversation("paper-1", title="Latency")

    assert [c.id for c in service.list_conversations("paper-1")] == [conversation.id]
    assert service.rename_conversation(conversation.id, "Scheduling").title == "Scheduling"
    with pytest.raises(ScopeError, match="blank"):
        service.rename_conversation(conversation.id, "  ")

    service.delete_conversation(conversation.id)
    assert service.list_conversations("paper-1") == ()
