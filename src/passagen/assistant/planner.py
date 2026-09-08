"""Deterministic first-version context planner.

Routing rules map a standalone question to an intent and the context sources an
answer may use. The LLM rewrite stage handles follow-up resolution and query
generation; this module deliberately stays deterministic so routing is
reproducible and testable offline.
"""

from __future__ import annotations

import hashlib
import unicodedata
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from passagen.assistant.schemas import (
    AnswerKind,
    ContextPlan,
    ContextSource,
    QuestionIntent,
)

_STRUCTURE_CUES = (
    "章节",
    "结构",
    "组织",
    "哪一节",
    "哪部分",
    "目录",
    "section",
    "outline",
    "structure",
    "organized",
)
_FACT_CUES = (
    "多少",
    "数值",
    "具体",
    "原文",
    "实验",
    "experiment",
    "workload",
    "dataset",
    "数据集",
    "exact",
    "quote",
    "verbatim",
    "how many",
    "how much",
)
_EXPLANATION_CUES = (
    "解释",
    "如何",
    "为什么",
    "原理",
    "explain",
    "how does",
    "why does",
)


class RewriteResult(BaseModel):
    """Wire contract of the rewrite stage LLM call."""

    model_config = ConfigDict(extra="forbid")

    standalone_question: str
    retrieval_queries: list[str] = Field(default_factory=list)
    requires_exact_quote: bool = False
    conversation_title: str | None = None


class QaSemanticRelation(StrEnum):
    EQUIVALENT = "equivalent"
    PARTIAL = "partial"
    DIFFERENT = "different"


class QaSemanticMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    relation: QaSemanticRelation
    confidence: Literal["low", "medium", "high"]


class QaSemanticDecision(BaseModel):
    """Bounded wire contract for comparing one question with QA candidates."""

    model_config = ConfigDict(extra="forbid")

    matches: list[QaSemanticMatch]

    @model_validator(mode="after")
    def _candidate_ids_are_unique(self) -> QaSemanticDecision:
        ids = [match.candidate_id for match in self.matches]
        if len(ids) != len(set(ids)):
            raise ValueError("semantic candidate ids must be unique")
        return self


def normalize_question(question: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", question).casefold().split())


def question_hash(normalized_question: str) -> str:
    return hashlib.sha256(normalized_question.encode("utf-8")).hexdigest()


def deterministic_plan(
    *,
    standalone_question: str,
    retrieval_queries: list[str],
    requires_exact_quote: bool,
    has_history: bool,
    paper_id: str,
    available_sources: set[ContextSource],
) -> ContextPlan:
    text = standalone_question.casefold()
    if requires_exact_quote:
        intent = QuestionIntent.FACT_LOOKUP
        sources = [ContextSource.RAW]
    elif _matches(text, _STRUCTURE_CUES) and ContextSource.OUTLINE in available_sources:
        intent = QuestionIntent.STRUCTURE
        sources = [ContextSource.OUTLINE]
    elif _matches(text, _FACT_CUES):
        intent = QuestionIntent.FACT_LOOKUP
        sources = [ContextSource.SUMMARY, ContextSource.RAW]
    elif _matches(text, _EXPLANATION_CUES):
        intent = QuestionIntent.EXPLANATION
        sources = [ContextSource.SUMMARY, ContextSource.RAW]
    else:
        intent = QuestionIntent.OVERVIEW
        sources = [ContextSource.SUMMARY]
    selected = [source for source in sources if source in available_sources]
    if not selected:
        selected = [ContextSource.SUMMARY]
    queries = list(retrieval_queries)
    if ContextSource.RAW in selected and not queries:
        queries = [standalone_question]
    if has_history:
        selected.insert(0, ContextSource.CONVERSATION)
    return ContextPlan(
        standalone_question=standalone_question,
        intent=intent,
        sources=selected,
        paper_ids=[paper_id],
        retrieval_queries=queries,
        requires_exact_quote=requires_exact_quote,
        answer_kind=AnswerKind.DIRECT,
    )


def _matches(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)
