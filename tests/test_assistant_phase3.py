import json
from collections.abc import Callable
from pathlib import Path

from support import (
    AssistantEnv,
    FakeProvider,
    assistant_env,
    assistant_service,
    scripted_responder,
)

from passagen.assistant import repository
from passagen.assistant.planner import normalize_question, question_hash
from passagen.assistant.schemas import ContextSource, TurnDisposition
from passagen.storage.database import connect_database


def _semantic_responder(
    env: AssistantEnv, relation: str, confidence: str = "high"
) -> Callable[[str], object]:
    base = scripted_responder(env)

    def respond(prompt: str) -> object:
        if "You conservatively compare" not in prompt:
            return base(prompt)
        payload = prompt.split("<candidates>", 1)[1].split("</candidates>", 1)[0].strip()
        candidates = json.loads(payload)
        return json.dumps(
            {
                "matches": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "relation": relation,
                        "confidence": confidence,
                    }
                    for candidate in candidates
                ]
            }
        )

    return respond


def test_exact_question_reuses_answer_without_answer_call(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id

    original = service.ask(conversation_id, "这篇论文要解决什么问题？")
    reused = service.ask(conversation_id, "这篇论文要解决什么问题？")

    assert reused.disposition is TurnDisposition.EXACT_REUSE
    assert reused.qa_record.id != original.qa_record.id
    assert reused.qa_record.context_plan.sources == [ContextSource.PREVIOUS_QA]
    assert reused.qa_record.context_plan.reuse_qa_id == original.qa_record.id
    assert len(provider.prompts) == 3


def test_force_regenerate_bypasses_exact_reuse(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id
    service.ask(conversation_id, "这篇论文要解决什么问题？")

    regenerated = service.ask(conversation_id, "这篇论文要解决什么问题？", force_regenerate=True)

    assert regenerated.disposition is TurnDisposition.GENERATED
    assert regenerated.qa_record.context_plan.reuse_qa_id is None
    assert len(provider.prompts) == 4


def test_high_confidence_semantic_equivalent_reuses_compatible_answer(
    tmp_path: Path,
) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(_semantic_responder(env, "equivalent"))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id
    original = service.ask(conversation_id, "What workload does the latency experiment use?")

    reused = service.ask(conversation_id, "Which workload was used to evaluate latency?")

    assert reused.disposition is TurnDisposition.SEMANTIC_REUSE
    assert reused.qa_record.context_plan.sources == [ContextSource.PREVIOUS_QA]
    assert reused.qa_record.context_plan.reuse_qa_id == original.qa_record.id
    assert len([prompt for prompt in provider.prompts if "Give a thorough" in prompt]) == 1


def test_partial_candidate_is_previous_qa_context_for_new_answer(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(_semantic_responder(env, "partial"))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id
    original = service.ask(conversation_id, "What workload does the latency experiment use?")

    generated = service.ask(
        conversation_id,
        "What workload does the latency experiment use and what are its limitations?",
    )

    assert generated.disposition is TurnDisposition.GENERATED
    assert generated.qa_record.context_plan.reuse_qa_id == original.qa_record.id
    assert ContextSource.PREVIOUS_QA in generated.qa_record.context_plan.sources
    answer_prompt = provider.prompts[-1]
    assert f"[source previous_qa qa_record_id={original.qa_record.id}]" in answer_prompt
    assert original.qa_record.answer.answer_markdown in answer_prompt


def test_non_high_confidence_equivalent_generates_without_previous_qa(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(_semantic_responder(env, "equivalent", "medium"))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id
    service.ask(conversation_id, "What workload does the latency experiment use?")

    generated = service.ask(conversation_id, "Which workload was used to evaluate latency?")

    assert generated.disposition is TurnDisposition.GENERATED
    assert generated.qa_record.context_plan.reuse_qa_id is None
    assert ContextSource.PREVIOUS_QA not in generated.qa_record.context_plan.sources


def test_incompatible_candidate_is_not_classified_or_reused(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(_semantic_responder(env, "equivalent"))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id
    original = service.ask(conversation_id, "What workload does the latency experiment use?")
    with connect_database(env.database_path) as connection:
        connection.execute(
            "UPDATE qa_records SET prompt_version = 'old' WHERE id = ?",
            (original.qa_record.id,),
        )

    generated = service.ask(conversation_id, "Which workload was used to evaluate latency?")

    assert generated.disposition is TurnDisposition.GENERATED
    assert not any("You conservatively compare" in prompt for prompt in provider.prompts)


def test_exact_candidate_with_invalid_citations_is_not_reused(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    provider = FakeProvider(scripted_responder(env))
    service = assistant_service(env, provider)
    conversation_id = service.create_conversation("paper-1").id
    original = service.ask(conversation_id, "What workload does the latency experiment use?")
    invalid_answer = original.qa_record.answer.model_dump(mode="json")
    invalid_answer["citations"][0]["artifact_sha256"] = "0" * 64
    with connect_database(env.database_path) as connection:
        connection.execute(
            "UPDATE qa_records SET answer_json = ? WHERE id = ?",
            (json.dumps(invalid_answer), original.qa_record.id),
        )

    generated = service.ask(conversation_id, "What workload does the latency experiment use?")

    assert generated.disposition is TurnDisposition.GENERATED
    assert generated.qa_record.context_plan.reuse_qa_id is None


def test_candidate_retrieval_is_bounded_to_same_paper(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))
    conversation_id = service.create_conversation("paper-1").id
    for index in range(3):
        service.ask(conversation_id, f"Question {index}?", force_regenerate=True)

    candidates = repository.find_qa_candidates(
        env.database_path,
        paper_id="paper-1",
        exclude_normalized_question_hash=question_hash(normalize_question("unseen")),
        pool_size=2,
    )

    assert len(candidates) == 2
    assert all(
        candidate.source_snapshot.paper is not None
        and candidate.source_snapshot.paper.paper_id == "paper-1"
        for candidate in candidates
    )


def test_source_status_reports_changed_artifact(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))
    conversation_id = service.create_conversation("paper-1").id
    record = service.ask(conversation_id, "这篇论文要解决什么问题？").qa_record

    assert service.source_status(record).stale is False
    with connect_database(env.database_path) as connection:
        connection.execute("UPDATE artifacts SET sha256 = ? WHERE id = 'art-summary'", ("0" * 64,))

    status = service.source_status(record)
    assert status.stale is True
    assert "summary_json_content_changed" in status.reasons


def test_raw_question_lazily_builds_fts_index(tmp_path: Path) -> None:
    env = assistant_env(tmp_path)
    service = assistant_service(env, FakeProvider(scripted_responder(env)))
    conversation_id = service.create_conversation("paper-1").id

    service.ask(conversation_id, "延迟实验使用的 workload 和具体数值是多少？")

    with connect_database(env.database_path) as connection:
        sections = connection.execute(
            "SELECT ordinal, extracted_artifact_sha256 FROM paper_sections ORDER BY ordinal"
        ).fetchall()
        hits = connection.execute(
            "SELECT count(*) FROM paper_sections_fts WHERE paper_sections_fts MATCH 'workload'"
        ).fetchone()[0]
    assert [row[0] for row in sections] == [0, 1, 2]
    assert {row[1] for row in sections} == {env.extracted_sha}
    assert hits == 1
