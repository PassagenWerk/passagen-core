from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from passagen.assistant import repository
from passagen.assistant.errors import (
    AnswerValidationError,
    ProviderCallError,
    ScopeError,
)
from passagen.assistant.schemas import ContextSource, MessageStatus, QuestionIntent
from passagen.assistant.service import ConversationService
from passagen.config import LlmSettings
from passagen.external.llm import LlmProviderError, LlmResponse
from passagen.parsing import ParsedPaper, ParsedSection
from passagen.stages.summarization.schema import (
    EvaluationResult,
    StructuredSummary,
    SummaryEvaluation,
    SummaryIdentity,
)
from passagen.storage.database import connect_database, initialize_database


@dataclass(frozen=True, slots=True)
class _Env:
    data_dir: Path
    database_path: Path
    summary_artifact_id: str
    summary_sha: str
    outline_artifact_id: str
    outline_sha: str
    extracted_artifact_id: str
    extracted_sha: str


class _FakeProvider:
    provider_name = "fake"
    model = "fake-model"

    def __init__(self, responder: Callable[[str], object]) -> None:
        self.responder = responder
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        self.prompts.append(prompt)
        outcome = self.responder(prompt)
        if isinstance(outcome, Exception):
            raise outcome
        return LlmResponse(
            content=str(outcome), input_tokens=5, output_tokens=10, finish_reason="stop"
        )


def _env(tmp_path: Path) -> _Env:
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    initialize_database(database_path)

    parsed = ParsedPaper(
        sections=(
            ParsedSection(title="1 Introduction", text="Scheduling latency matters.", pages=(1,)),
            ParsedSection(
                title="4.2 Latency Evaluation",
                text="We measure tail latency under the RNIC workload. Latency dropped to 12 ms.",
                pages=(5, 6),
            ),
            ParsedSection(
                title="5 Related Work",
                text="Prior schedulers are surveyed here.",
                pages=(7,),
            ),
        ),
        parser="fake",
    )
    summary = StructuredSummary(
        identity=SummaryIdentity(title="A Paper"),
        evaluation=SummaryEvaluation(
            results=[
                EvaluationResult(
                    metric="latency",
                    subject="scheduler",
                    subject_value="12 ms",
                    evidence_pages=[5],
                )
            ]
        ),
    )
    outline = "# 1 Introduction\n# 4 Evaluation\n## 4.2 Latency Evaluation\n"

    paper_dir = data_dir / "papers" / "paper-1"
    paper_dir.mkdir(parents=True)
    contents = {
        "extracted.json": parsed.model_dump_json(),
        "summary.json": summary.model_dump_json(),
        "outline.md": outline,
    }
    for name, content in contents.items():
        (paper_dir / name).write_text(content, encoding="utf-8")

    def sha(name: str) -> str:
        return hashlib.sha256(contents[name].encode()).hexdigest()

    with connect_database(database_path) as connection:
        connection.execute(
            "INSERT INTO papers (id, original_filename, pdf_sha256, status) "
            "VALUES ('paper-1', 'a.pdf', ?, 'outlined')",
            ("f" * 64,),
        )
        for artifact_id, kind, name, version in (
            ("art-extracted", "extracted_json", "extracted.json", "1"),
            ("art-summary", "summary_json", "summary.json", "2"),
            ("art-outline", "outline_md", "outline.md", "1"),
        ):
            connection.execute(
                "INSERT INTO artifacts (id, paper_id, kind, path, version, sha256) "
                "VALUES (?, 'paper-1', ?, ?, ?, ?)",
                (artifact_id, kind, f"papers/paper-1/{name}", version, sha(name)),
            )
    return _Env(
        data_dir=data_dir,
        database_path=database_path,
        summary_artifact_id="art-summary",
        summary_sha=sha("summary.json"),
        outline_artifact_id="art-outline",
        outline_sha=sha("outline.md"),
        extracted_artifact_id="art-extracted",
        extracted_sha=sha("extracted.json"),
    )


def _good_answer(env: _Env, question: str, citation: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "standalone_question": question,
            "intent": "fact_lookup",
            "answer_markdown": "The latency workload was RNIC [c-1].",
            "claims": [{"text": "The latency workload was RNIC.", "citation_ids": ["c-1"]}],
            "citations": [
                citation
                or {
                    "citation_id": "c-1",
                    "paper_id": "paper-1",
                    "artifact_kind": "summary_json",
                    "artifact_id": env.summary_artifact_id,
                    "artifact_sha256": env.summary_sha,
                    "summary_path": "evaluation.results[0]",
                    "page_start": 5,
                }
            ],
            "limitations": [],
            "follow_up_questions": [],
        }
    )


def _raw_citation(env: _Env) -> dict[str, object]:
    return {
        "citation_id": "c-1",
        "paper_id": "paper-1",
        "artifact_kind": "extracted_json",
        "artifact_id": env.extracted_artifact_id,
        "artifact_sha256": env.extracted_sha,
        "section": "4.2 Latency Evaluation",
        "page_start": 5,
        "page_end": 6,
        "excerpt": "Latency dropped to 12 ms.",
    }


def _outline_citation(env: _Env) -> dict[str, object]:
    return {
        "citation_id": "c-1",
        "paper_id": "paper-1",
        "artifact_kind": "outline_md",
        "artifact_id": env.outline_artifact_id,
        "artifact_sha256": env.outline_sha,
        "section": "4 Evaluation",
    }


def _responder(
    env: _Env,
    *,
    rewrite_question: str | None = None,
    citation: dict[str, object] | None = None,
    first_answer: str | None = None,
    repair_answer: str | None = None,
    fail_rewrite: bool = False,
) -> Callable[[str], object]:
    answers = 0
    standalone = ""

    def respond(prompt: str) -> object:
        nonlocal answers, standalone
        if "You rewrite a question" in prompt:
            if fail_rewrite:
                return LlmProviderError("provider timeout")
            if rewrite_question is not None:
                standalone = rewrite_question
            else:
                standalone = (
                    prompt.split("<user_question>", 1)[1].split("</user_question>", 1)[0].strip()
                )
            return json.dumps(
                {
                    "standalone_question": standalone,
                    "retrieval_queries": ["latency workload"],
                    "requires_exact_quote": False,
                }
            )
        if "failed validation" in prompt:
            return repair_answer or _good_answer(env, standalone, citation)
        answers += 1
        if first_answer is not None and answers == 1:
            return first_answer
        return _good_answer(env, standalone, citation)

    return respond


def _service(env: _Env, provider: _FakeProvider) -> ConversationService:
    return ConversationService(env.database_path, env.data_dir, LlmSettings(), provider=provider)


def _create(service: ConversationService) -> str:
    return service.create_conversation("paper-1", title="Latency").id


def test_overview_question_uses_summary_only(tmp_path: Path) -> None:
    env = _env(tmp_path)
    provider = _FakeProvider(_responder(env))
    service = _service(env, provider)
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
    env = _env(tmp_path)
    provider = _FakeProvider(_responder(env, citation=_outline_citation(env)))
    service = _service(env, provider)

    turn = service.ask(_create(service), "文章是怎么组织的？评估部分在哪一节？")

    assert turn.qa_record.context_plan.sources == [ContextSource.OUTLINE]
    assert "[source outline_md" in provider.prompts[-1]


def test_fact_question_adds_raw_sections_without_full_text(tmp_path: Path) -> None:
    env = _env(tmp_path)
    provider = _FakeProvider(_responder(env, citation=_raw_citation(env)))
    service = _service(env, provider)

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
    env = _env(tmp_path)
    provider = _FakeProvider(_responder(env, rewrite_question="What was the second experiment?"))
    service = _service(env, provider)
    conversation_id = _create(service)
    service.ask(conversation_id, "这篇论文做了哪些实验？")

    turn = service.ask(conversation_id, "那它的第二个实验呢？")

    assert turn.qa_record.standalone_question == "What was the second experiment?"
    assert turn.qa_record.context_plan.sources[0] is ContextSource.CONVERSATION
    rewrite_prompt = provider.prompts[-2]
    assert "这篇论文做了哪些实验？" in rewrite_prompt


def test_provider_failure_keeps_question_and_allows_retry(tmp_path: Path) -> None:
    env = _env(tmp_path)
    failing = _FakeProvider(_responder(env, fail_rewrite=True))
    service = _service(env, failing)
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

    working = _FakeProvider(_responder(env))
    retry_service = _service(env, working)
    turn = retry_service.ask(conversation_id, "这篇论文要解决什么问题，主要贡献是什么？")
    assert turn.qa_record.id
    assert len(service.get_conversation(conversation_id).messages) == 4


def test_invalid_citation_is_repaired(tmp_path: Path) -> None:
    env = _env(tmp_path)
    bad_answer = _good_answer(env, "具体数值是多少？")
    bad_answer = bad_answer.replace(env.summary_sha, "0" * 64)
    provider = _FakeProvider(_responder(env, first_answer=bad_answer))
    service = _service(env, provider)

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
    env = _env(tmp_path)
    question = "具体数值是多少？"
    bad_answer = _good_answer(env, question).replace(env.summary_sha, "0" * 64)
    provider = _FakeProvider(_responder(env, first_answer=bad_answer, repair_answer=bad_answer))
    service = _service(env, provider)
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


def test_answer_diagnostics_are_written_to_run_directory(tmp_path: Path) -> None:
    env = _env(tmp_path)
    provider = _FakeProvider(_responder(env))
    service = _service(env, provider)

    turn = service.ask(_create(service), "这篇论文要解决什么问题，主要贡献是什么？")

    call_dirs = sorted((env.data_dir / "runs" / turn.run_id / "llm").iterdir())
    assert len(call_dirs) == 2
    for call_dir in call_dirs:
        assert (call_dir / "request.json").is_file()
        assert (call_dir / "response.json").is_file()


def test_ask_requires_processed_paper(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with connect_database(env.database_path) as connection:
        connection.execute(
            "INSERT INTO papers (id, original_filename, pdf_sha256) VALUES ('paper-2', 'b.pdf', ?)",
            ("e" * 64,),
        )
    service = _service(env, _FakeProvider(_responder(env)))
    conversation_id = service.create_conversation("paper-2").id

    with pytest.raises(ScopeError, match="extracted_json"):
        service.ask(conversation_id, "问题？")


def test_ask_rejects_blank_question_and_missing_conversation(tmp_path: Path) -> None:
    env = _env(tmp_path)
    service = _service(env, _FakeProvider(_responder(env)))

    with pytest.raises(ScopeError, match="blank"):
        service.ask(_create(service), "   ")
    with pytest.raises(ScopeError, match="not found"):
        service.ask("missing-conversation", "问题？")


def test_conversation_management(tmp_path: Path) -> None:
    env = _env(tmp_path)
    service = _service(env, _FakeProvider(_responder(env)))
    conversation = service.create_conversation("paper-1", title="Latency")

    assert [c.id for c in service.list_conversations("paper-1")] == [conversation.id]
    assert service.rename_conversation(conversation.id, "Scheduling").title == "Scheduling"
    with pytest.raises(ScopeError, match="blank"):
        service.rename_conversation(conversation.id, "  ")

    service.delete_conversation(conversation.id)
    assert service.list_conversations("paper-1") == ()
