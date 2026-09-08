from __future__ import annotations

import pytest

from passagen.assistant.citations import EvidenceIndex, PaperEvidenceIndex
from passagen.assistant.errors import CitationValidationError, ContextPlanError
from passagen.assistant.planner import collection_deterministic_plan
from passagen.assistant.retrieval import PaperCandidate, select_papers
from passagen.assistant.schemas import (
    AnswerKind,
    ArtifactRef,
    Citation,
    CitationArtifactKind,
    ContextSource,
    PaperSourceSnapshot,
    QuestionIntent,
    StructuredAnswer,
)
from passagen.research.schemas import CollectionReport, ReportKind, SynthesisCoverage
from passagen.stages.summarization.schema import StructuredSummary, SummaryIdentity

_ALL_SOURCES = {
    ContextSource.CONVERSATION,
    ContextSource.PREVIOUS_QA,
    ContextSource.COLLECTION_SUMMARY,
    ContextSource.PAPER_SUMMARIES,
    ContextSource.RAW,
}


def test_collection_plan_defaults_to_synthesis_sources() -> None:
    plan = collection_deterministic_plan(
        standalone_question="What do these papers have in common?",
        retrieval_queries=[],
        requires_exact_quote=False,
        has_history=False,
        available_sources=set(_ALL_SOURCES),
    )
    assert plan.intent is QuestionIntent.SYNTHESIS
    assert plan.answer_kind is AnswerKind.SYNTHESIS
    assert ContextSource.RAW not in plan.sources


def test_collection_plan_escalates_to_raw_for_facts_and_comparisons() -> None:
    fact = collection_deterministic_plan(
        standalone_question="What workload did the latency experiment use?",
        retrieval_queries=["latency workload"],
        requires_exact_quote=False,
        has_history=False,
        available_sources=set(_ALL_SOURCES),
    )
    assert fact.intent is QuestionIntent.FACT_LOOKUP
    assert ContextSource.RAW in fact.sources

    comparison = collection_deterministic_plan(
        standalone_question="How do the papers compare on evaluation?",
        retrieval_queries=["evaluation"],
        requires_exact_quote=False,
        has_history=False,
        available_sources=set(_ALL_SOURCES),
    )
    assert comparison.intent is QuestionIntent.COMPARISON
    assert comparison.answer_kind is AnswerKind.COMPARATIVE
    assert ContextSource.RAW in comparison.sources


def test_collection_plan_filters_unavailable_sources() -> None:
    plan = collection_deterministic_plan(
        standalone_question="What do these papers have in common?",
        retrieval_queries=[],
        requires_exact_quote=False,
        has_history=False,
        available_sources={ContextSource.PAPER_SUMMARIES},
    )
    assert plan.sources == [ContextSource.PAPER_SUMMARIES]

    with pytest.raises(ContextPlanError):
        collection_deterministic_plan(
            standalone_question="Anything?",
            retrieval_queries=[],
            requires_exact_quote=False,
            has_history=False,
            available_sources=set(),
        )


def test_select_papers_is_bounded_and_relevant() -> None:
    candidates = [
        PaperCandidate("paper-a", "Fast Scheduler", "latency workload scheduler"),
        PaperCandidate("paper-b", "Better Compiler", "compiler optimization passes"),
        PaperCandidate("paper-c", "Graph Partitioner", "graph partitioning kernels"),
        PaperCandidate("paper-d", "Consensus", "consensus protocol latency"),
    ]
    selected = select_papers(candidates, ["latency"], max_papers=2)
    assert selected == ["paper-a", "paper-d"]
    assert select_papers(candidates, ["latency"], max_papers=1) == ["paper-a"]
    assert select_papers(candidates, [], max_papers=2) == ["paper-a", "paper-b"]
    assert select_papers(candidates, ["absent"], max_papers=2) == ["paper-a", "paper-b"]
    assert select_papers(candidates[:2], ["latency"], max_papers=4) == [
        "paper-a",
        "paper-b",
    ]


def _paper_snapshot(paper_id: str) -> PaperSourceSnapshot:
    return PaperSourceSnapshot(
        paper_id=paper_id,
        title=paper_id,
        status="summarized",
        artifacts=[
            ArtifactRef(
                artifact_id=f"art-{paper_id}",
                kind="summary_json",
                schema_version="1",
                sha256="a" * 64,
            )
        ],
    )


def _answer(paper_id: str) -> StructuredAnswer:
    return StructuredAnswer(
        standalone_question="q",
        intent=QuestionIntent.SYNTHESIS,
        answer_markdown="a [c-1]",
        citations=[
            Citation(
                citation_id="c-1",
                paper_id=paper_id,
                artifact_kind=CitationArtifactKind.SUMMARY,
                artifact_id=f"art-{paper_id}",
                artifact_sha256="a" * 64,
                summary_path="identity.title",
            )
        ],
    )


def test_multi_paper_citation_validation() -> None:
    summary = StructuredSummary(identity=SummaryIdentity(title="T"))
    index = EvidenceIndex(
        papers=(
            PaperEvidenceIndex(
                snapshot=_paper_snapshot("paper-a"), summary=summary, outline=None, parsed=None
            ),
        )
    )
    # Single-paper behavior is unchanged.
    from passagen.assistant.citations import validate_answer_citations

    validate_answer_citations(_answer("paper-a"), index.papers[0])
    validate_answer_citations(_answer("paper-a"), index)
    with pytest.raises(CitationValidationError):
        validate_answer_citations(_answer("paper-b"), index)


def test_report_schema_rejects_dangling_claim_citations() -> None:
    with pytest.raises(ValueError, match="unknown citations"):
        CollectionReport.model_validate(
            {
                "kind": ReportKind.REVIEW,
                "title": "t",
                "coverage": SynthesisCoverage(included_paper_ids=["paper-a"]).model_dump(),
                "sections": [{"heading": "h", "body_markdown": "b"}],
                "claims": [{"text": "x", "citation_ids": ["missing"]}],
                "citations": [
                    {
                        "citation_id": "c-1",
                        "paper_id": "paper-a",
                        "artifact_kind": "summary_json",
                        "artifact_sha256": "a" * 64,
                        "summary_path": "identity.title",
                    }
                ],
            }
        )
