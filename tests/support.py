"""Shared fake provider and library builders for assistant service tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
class AssistantEnv:
    data_dir: Path
    database_path: Path
    summary_artifact_id: str
    summary_sha: str
    outline_artifact_id: str
    outline_sha: str
    extracted_artifact_id: str
    extracted_sha: str


class FakeProvider:
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


def assistant_env(tmp_path: Path) -> AssistantEnv:
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
    return AssistantEnv(
        data_dir=data_dir,
        database_path=database_path,
        summary_artifact_id="art-summary",
        summary_sha=sha("summary.json"),
        outline_artifact_id="art-outline",
        outline_sha=sha("outline.md"),
        extracted_artifact_id="art-extracted",
        extracted_sha=sha("extracted.json"),
    )


def good_answer(env: AssistantEnv, question: str, citation: dict[str, object] | None = None) -> str:
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


def raw_citation(env: AssistantEnv) -> dict[str, object]:
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


def outline_citation(env: AssistantEnv) -> dict[str, object]:
    return {
        "citation_id": "c-1",
        "paper_id": "paper-1",
        "artifact_kind": "outline_md",
        "artifact_id": env.outline_artifact_id,
        "artifact_sha256": env.outline_sha,
        "section": "4 Evaluation",
    }


def scripted_responder(
    env: AssistantEnv,
    *,
    rewrite_question: str | None = None,
    citation: dict[str, object] | None = None,
    first_answer: str | None = None,
    repair_answer: str | None = None,
    fail_rewrite: bool = False,
    conversation_title: str | None = "Latency workload",
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
                    "conversation_title": conversation_title,
                }
            )
        if "failed validation" in prompt:
            return repair_answer or good_answer(env, standalone, citation)
        answers += 1
        if first_answer is not None and answers == 1:
            return first_answer
        return good_answer(env, standalone, citation)

    return respond


def assistant_service(env: AssistantEnv, provider: FakeProvider) -> ConversationService:
    return ConversationService(env.database_path, env.data_dir, LlmSettings(), provider=provider)
