"""Local evaluation question set contract.

The evaluation set drives offline planner and routing checks. It never calls a
real LLM; each question declares the intent and context sources a correct
implementation is expected to use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from passagen.assistant.schemas import (
    ContextSource,
    ConversationScope,
    NonBlankStr,
    QuestionIntent,
)
from passagen.assistant.versions import EVAL_SET_SCHEMA_VERSION


class EvalQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    scope: ConversationScope
    question: NonBlankStr
    history: list[NonBlankStr] = Field(default_factory=list)
    expected_intent: QuestionIntent
    expected_sources: list[ContextSource] = Field(min_length=1)
    notes: str | None = None


class EvalQuestionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = EVAL_SET_SCHEMA_VERSION
    questions: list[EvalQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def _question_ids_are_unique(self) -> EvalQuestionSet:
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation question ids must be unique")
        return self


def load_eval_questions(path: Path) -> EvalQuestionSet:
    return EvalQuestionSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
