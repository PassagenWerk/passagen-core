from __future__ import annotations

from passagen.assistant.planner import (
    RewriteResult,
    deterministic_plan,
    normalize_question,
    question_hash,
)
from passagen.assistant.schemas import ContextPlan, ContextSource, QuestionIntent

PAPER_SOURCES = {
    ContextSource.CONVERSATION,
    ContextSource.SUMMARY,
    ContextSource.OUTLINE,
    ContextSource.RAW,
}


def _plan(
    question: str,
    *,
    retrieval_queries: list[str] | None = None,
    requires_exact_quote: bool = False,
    has_history: bool = False,
    available_sources: set[ContextSource] | None = None,
) -> ContextPlan:
    return deterministic_plan(
        standalone_question=question,
        retrieval_queries=retrieval_queries if retrieval_queries is not None else ["latency"],
        requires_exact_quote=requires_exact_quote,
        has_history=has_history,
        paper_id="paper-1",
        available_sources=available_sources or PAPER_SOURCES,
    )


def test_overview_questions_route_to_summary() -> None:
    plan = _plan("这篇论文要解决什么问题，主要贡献是什么？")

    assert plan.intent is QuestionIntent.OVERVIEW
    assert plan.sources == [ContextSource.SUMMARY]


def test_structure_questions_route_to_outline() -> None:
    plan = _plan("文章是怎么组织的？评估部分在哪一节？")

    assert plan.intent is QuestionIntent.STRUCTURE
    assert plan.sources == [ContextSource.OUTLINE]


def test_structure_questions_fall_back_to_summary_without_outline() -> None:
    plan = _plan(
        "Which section contains the evaluation?",
        available_sources={ContextSource.SUMMARY, ContextSource.RAW},
    )

    assert plan.sources == [ContextSource.SUMMARY]


def test_fact_questions_route_to_summary_and_raw() -> None:
    plan = _plan("延迟实验使用的是什么 workload，具体数值是多少？")

    assert plan.intent is QuestionIntent.FACT_LOOKUP
    assert plan.sources == [ContextSource.SUMMARY, ContextSource.RAW]


def test_exact_quote_questions_route_to_raw_only() -> None:
    plan = _plan(
        "Quote the exact cache eviction policy.",
        requires_exact_quote=True,
    )

    assert plan.intent is QuestionIntent.FACT_LOOKUP
    assert plan.sources == [ContextSource.RAW]


def test_explanation_questions_combine_summary_and_raw() -> None:
    plan = _plan("解释一下它的调度设计是如何降低尾延迟的。")

    assert plan.intent is QuestionIntent.EXPLANATION
    assert plan.sources == [ContextSource.SUMMARY, ContextSource.RAW]


def test_follow_up_questions_include_conversation_history() -> None:
    plan = _plan("那它的第二个实验呢？", has_history=True)

    assert plan.sources[0] is ContextSource.CONVERSATION
    assert plan.intent is QuestionIntent.FACT_LOOKUP


def test_raw_routing_falls_back_to_question_as_retrieval_query() -> None:
    plan = _plan("具体数值是多少？", retrieval_queries=[])

    assert plan.retrieval_queries == ["具体数值是多少？"]


def test_normalize_question_is_case_and_whitespace_insensitive() -> None:
    assert normalize_question("  What   Workload  WAS used? ") == "what workload was used?"
    assert question_hash(normalize_question("What workload was used?")) == question_hash(
        normalize_question("what workload was used?")
    )


def test_rewrite_result_wire_contract() -> None:
    result = RewriteResult.model_validate_json(
        '{"standalone_question": "q", "retrieval_queries": ["latency"], '
        '"requires_exact_quote": true}'
    )

    assert result.requires_exact_quote is True
