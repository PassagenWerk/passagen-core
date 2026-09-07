from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from passagen.assistant.evaluation import EvalQuestionSet, load_eval_questions
from passagen.assistant.schemas import ContextSource

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant" / "eval_questions.json"


def test_eval_question_fixture_is_valid() -> None:
    question_set = load_eval_questions(FIXTURE_PATH)

    assert question_set.questions


def test_eval_question_fixture_covers_all_context_categories() -> None:
    question_set = load_eval_questions(FIXTURE_PATH)
    questions = question_set.questions

    def has(**predicates: object) -> bool:
        return any(
            all(getattr(question, key) == value for key, value in predicates.items())
            for question in questions
        )

    assert any(question.expected_sources == [ContextSource.SUMMARY] for question in questions), (
        "summary-only routing is not covered"
    )
    assert any(question.expected_sources == [ContextSource.OUTLINE] for question in questions), (
        "outline routing is not covered"
    )
    assert any(ContextSource.RAW in question.expected_sources for question in questions), (
        "raw routing is not covered"
    )
    assert any(len(question.expected_sources) > 1 for question in questions), (
        "combined context is not covered"
    )
    assert any(question.history for question in questions), "follow-up turns are not covered"
    assert any(ContextSource.PREVIOUS_QA in question.expected_sources for question in questions), (
        "duplicate-question reuse is not covered"
    )
    assert has(scope="collection"), "collection scope is not covered"
    assert has(scope="paper"), "paper scope is not covered"


def test_eval_questions_declare_expected_context(tmp_path: Path) -> None:
    question_set = load_eval_questions(FIXTURE_PATH)
    for question in question_set.questions:
        assert question.expected_sources, question.id

    invalid = {
        "schema_version": "1",
        "questions": [
            {
                "id": "broken-01",
                "scope": "paper",
                "question": "q",
                "expected_intent": "overview",
                "expected_sources": [],
            }
        ],
    }
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_eval_questions(invalid_path)


def test_eval_question_ids_are_unique(tmp_path: Path) -> None:
    question = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["questions"][0]
    duplicated = tmp_path / "duplicated.json"
    duplicated.write_text(
        json.dumps({"schema_version": "1", "questions": [question, question]}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unique"):
        EvalQuestionSet.model_validate_json(duplicated.read_text(encoding="utf-8"))
