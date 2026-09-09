from __future__ import annotations

import json
from pathlib import Path

import pytest
from collection_support import (
    CollectionEnv,
    collection_answer,
    collection_env,
    collection_responder,
    summary_citation,
)
from support import FakeProvider

from passagen.assistant.errors import AnswerValidationError, ScopeError, StaleSourceError
from passagen.assistant.schemas import (
    ConversationScope,
    MessageStatus,
    TurnDisposition,
)
from passagen.assistant.service import ConversationService
from passagen.config import AssistantSettings, LlmSettings
from passagen.storage.database import connect_database


def _service(
    env: CollectionEnv,
    provider: FakeProvider,
    assistant_settings: AssistantSettings | None = None,
) -> ConversationService:
    return ConversationService(
        env.database_path,
        env.data_dir,
        LlmSettings(),
        assistant_settings,
        provider=provider,
    )


def _rewrite_summary(env: CollectionEnv, paper_id: str, value: str = "99 ms") -> None:
    path = env.data_dir / "papers" / paper_id / "summary.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    content["evaluation"]["results"][0]["subject_value"] = value
    updated = json.dumps(content, ensure_ascii=False)
    path.write_text(updated, encoding="utf-8")
    import hashlib

    sha = hashlib.sha256(updated.encode()).hexdigest()
    with connect_database(env.database_path) as connection:
        connection.execute(
            "UPDATE artifacts SET sha256 = ? WHERE id = ?",
            (sha, env.artifact_ids[paper_id]["summary_json"]),
        )
    # Model a rebuilt artifact: later fake answers cite the new content hash.
    env.artifact_shas[paper_id]["summary_json"] = sha


def test_create_and_list_collection_conversations(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    service = _service(env, FakeProvider(lambda prompt: ""))

    conversation = service.create_conversation(collection_id=env.collection_id, title="Group")

    assert conversation.scope is ConversationScope.COLLECTION
    assert conversation.collection_id == env.collection_id
    assert conversation.paper_id is None
    listed = service.list_conversations(collection_id=env.collection_id)
    assert [item.id for item in listed] == [conversation.id]
    assert service.list_conversations("paper-a") == ()


def test_conversation_scope_requires_exactly_one_target(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    service = _service(env, FakeProvider(lambda prompt: ""))

    with pytest.raises(ScopeError):
        service.create_conversation()
    with pytest.raises(ScopeError):
        service.create_conversation("paper-a", collection_id=env.collection_id)
    with pytest.raises(ScopeError):
        service.list_conversations()
    with pytest.raises(ScopeError):
        service.list_conversations("paper-a", collection_id=env.collection_id)


def test_collection_turn_generates_scoped_qa_record(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)

    turn = service.ask(conversation.id, "这些论文有什么共同主题？")

    assert turn.disposition is TurnDisposition.GENERATED
    assert turn.answer_message.status is MessageStatus.COMPLETED
    record = turn.qa_record
    assert record.source_snapshot.scope is ConversationScope.COLLECTION
    assert record.source_snapshot.collection is not None
    assert record.source_snapshot.collection.collection_id == env.collection_id
    assert set(record.source_snapshot.paper_ids()) == set(env.paper_ids)
    assert set(record.context_plan.paper_ids) == set(env.paper_ids)
    assert record.answer.citations[0].paper_id in env.paper_ids
    run = service.get_generation_run(turn.run_id)
    assert run.status == "completed"
    assert run.collection_id == env.collection_id
    stages = [call.stage for call in service.list_generation_llm_calls(turn.run_id)]
    assert stages == ["rewrite", "answer"]


def test_collection_two_level_retrieval_selects_relevant_papers(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider, AssistantSettings(collection_max_selected_papers=1))
    conversation = service.create_conversation(collection_id=env.collection_id)

    turn = service.ask(conversation.id, "这些论文的延迟实验用了什么负载？")

    assert turn.qa_record.context_plan.paper_ids == ["paper-a"]
    answer_prompt = provider.prompts[-1]
    assert "RNIC workload" in answer_prompt
    assert "Compiler optimization passes" not in answer_prompt
    assert "Graph partitioning" not in answer_prompt


def test_collection_citation_outside_scope_is_repaired(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    bad = collection_answer(
        env,
        "placeholder",
        citations=[{**summary_citation(env, "paper-a"), "paper_id": "paper-outside"}],
    )
    provider = FakeProvider(collection_responder(env, first_answer=bad))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)

    turn = service.ask(conversation.id, "这些论文有什么共同主题？")

    assert turn.answer_message.status is MessageStatus.COMPLETED
    stages = [call.stage for call in service.list_generation_llm_calls(turn.run_id)]
    assert stages == ["rewrite", "answer", "repair"]
    assert (
        "A collection_synthesis source is derived context and is not directly citable"
        in provider.prompts[-1]
    )


def test_collection_citation_outside_scope_fails_after_one_repair(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    bad = collection_answer(
        env,
        "placeholder",
        citations=[{**summary_citation(env, "paper-a"), "paper_id": "paper-outside"}],
    )
    provider = FakeProvider(collection_responder(env, first_answer=bad, repair_answer=bad))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)

    with pytest.raises(AnswerValidationError):
        service.ask(conversation.id, "这些论文有什么共同主题？")

    detail = service.get_conversation(conversation.id)
    assert detail.messages[-1].status is MessageStatus.FAILED
    assert detail.messages[0].content == "这些论文有什么共同主题？"
    run = service.get_generation_run(detail.messages[-1].run_id or "")
    assert run.status == "failed"
    assert run.error_code == "invalid_answer"


def test_collection_exact_question_reuse_across_conversations(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider)
    first = service.create_conversation(collection_id=env.collection_id)
    second = service.create_conversation(collection_id=env.collection_id)

    first_turn = service.ask(first.id, "这些论文有什么共同主题？")
    second_turn = service.ask(second.id, "这些论文有什么共同主题？")

    assert first_turn.disposition is TurnDisposition.GENERATED
    assert second_turn.disposition is TurnDisposition.EXACT_REUSE
    answer_calls = [prompt for prompt in provider.prompts if "<sources>" in prompt]
    assert len(answer_calls) == 1
    assert second_turn.qa_record.context_plan.reuse_qa_id == first_turn.qa_record.id


def test_collection_answer_goes_stale_when_a_summary_changes(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)
    turn = service.ask(conversation.id, "这些论文有什么共同主题？")

    assert service.source_status(turn.qa_record).stale is False
    _rewrite_summary(env, "paper-a")

    status = service.source_status(turn.qa_record)
    assert status.stale is True
    assert "paper-a:summary_json_content_changed" in status.reasons


def test_collection_stale_record_is_not_reused(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)
    service.ask(conversation.id, "这些论文有什么共同主题？")
    _rewrite_summary(env, "paper-a")

    turn = service.ask(conversation.id, "这些论文有什么共同主题？")

    assert turn.disposition is TurnDisposition.GENERATED
    answer_calls = [prompt for prompt in provider.prompts if "<sources>" in prompt]
    assert len(answer_calls) == 2


def test_queued_collection_turn_rejects_changed_sources(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)
    submission = service.submit_turn(conversation.id, "这些论文有什么共同主题？")
    _rewrite_summary(env, "paper-a")

    with pytest.raises(StaleSourceError):
        service.execute_turn(submission.run_id)

    detail = service.get_conversation(conversation.id)
    assert detail.messages[-1].status is MessageStatus.FAILED
    assert service.get_generation_run(submission.run_id).status == "failed"


def test_collection_turn_uses_matching_synthesis(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    _run_synthesis(env)
    provider = FakeProvider(collection_responder(env))
    service = _service(env, provider)
    conversation = service.create_conversation(collection_id=env.collection_id)

    turn = service.ask(conversation.id, "这些论文有什么共同主题？")

    snapshot = turn.qa_record.source_snapshot.collection
    assert snapshot is not None
    assert snapshot.synthesis is not None
    answer_prompt = provider.prompts[-1]
    assert "[source collection_synthesis" in answer_prompt
    assert "citation_policy=embedded_paper_citations_only" in answer_prompt
    assert "A collection_synthesis source is derived context and is not directly citable" in (
        answer_prompt
    )


def test_collection_turn_accepts_embedded_synthesis_citation_outside_retrieval(
    tmp_path: Path,
) -> None:
    env = collection_env(tmp_path)
    _run_synthesis(env)
    provider = FakeProvider(collection_responder(env, citations=[summary_citation(env, "paper-b")]))
    service = _service(env, provider, AssistantSettings(collection_max_selected_papers=1))
    conversation = service.create_conversation(collection_id=env.collection_id)

    turn = service.ask(conversation.id, "这些论文的延迟实验用了什么负载？")

    assert turn.qa_record.context_plan.paper_ids == ["paper-a"]
    assert turn.qa_record.answer.citations[0].paper_id == "paper-b"


def _run_synthesis(env: CollectionEnv) -> None:
    from passagen.research import CollectionSynthesisService

    def respond(prompt: str) -> object:
        citations = [
            {
                "citation_id": f"c-{index}",
                "paper_id": paper_id,
                "artifact_kind": "summary_json",
                "artifact_id": env.artifact_ids[paper_id]["summary_json"],
                "artifact_sha256": env.artifact_shas[paper_id]["summary_json"],
                "summary_path": "identity.title",
            }
            for index, paper_id in enumerate(env.paper_ids)
        ]
        return json.dumps(
            {
                "schema_version": "2",
                "executive_overview": "The collection studies systems.",
                "paper_roles": [
                    {
                        "paper_id": paper_id,
                        "role": "Systems evidence",
                        "contribution": "Contributes a systems result.",
                        "citation_ids": [citations[index]["citation_id"]],
                    }
                    for index, paper_id in enumerate(env.paper_ids)
                ],
                "themes": [
                    {
                        "name": "Systems",
                        "description": "All papers study systems.",
                        "paper_ids": list(env.paper_ids),
                        "citation_ids": [citation["citation_id"] for citation in citations],
                    }
                ],
                "comparison_matrix": {"dimensions": [], "rows": []},
                "claims": [
                    {
                        "text": "The collection studies systems.",
                        "citation_ids": [citation["citation_id"] for citation in citations],
                    }
                ],
                "citations": citations,
                "coverage": {
                    "included_paper_ids": list(env.paper_ids),
                    "missing_summary_paper_ids": [],
                    "partial": False,
                },
            }
        )

    provider = FakeProvider(respond)
    CollectionSynthesisService(
        env.database_path, env.data_dir, LlmSettings(), provider=provider
    ).synthesize(env.collection_id)
