from __future__ import annotations

import pytest
from pydantic import ValidationError

from passagen.assistant import (
    AnswerClaim,
    ArtifactRef,
    Citation,
    CitationArtifactKind,
    CollectionSourceSnapshot,
    ContextPlan,
    ContextSource,
    Conversation,
    PaperSourceSnapshot,
    QaRecord,
    QuestionIntent,
    SourceSnapshot,
    StructuredAnswer,
    source_fingerprint,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _artifact_ref(kind: str = "summary_json", sha256: str = SHA_A) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"artifact-{kind}",
        kind=kind,
        schema_version="1",
        sha256=sha256,
    )


def _paper_snapshot(paper_id: str = "paper-1") -> PaperSourceSnapshot:
    return PaperSourceSnapshot(
        paper_id=paper_id,
        title="A Paper",
        status="outlined",
        artifacts=[_artifact_ref("summary_json"), _artifact_ref("outline_md", SHA_B)],
    )


def _snapshot(**overrides: object) -> SourceSnapshot:
    values: dict[str, object] = {
        "scope": "paper",
        "paper": _paper_snapshot(),
        "context_builder_version": "1",
        "retrieval_version": "1",
        "prompt_version": "1",
        "answer_schema_version": "1",
    }
    values.update(overrides)
    return SourceSnapshot.model_validate(values)


def _citation(citation_id: str = "citation-1", paper_id: str = "paper-1") -> Citation:
    return Citation(
        citation_id=citation_id,
        paper_id=paper_id,
        artifact_kind=CitationArtifactKind.SUMMARY,
        artifact_id="artifact-summary_json",
        artifact_sha256=SHA_A,
        summary_path="evaluation.results[0]",
        page_start=5,
        page_end=6,
        excerpt="latency dropped to 12 ms",
    )


def _answer(**overrides: object) -> StructuredAnswer:
    values: dict[str, object] = {
        "standalone_question": "What workload was used for the latency result?",
        "intent": "fact_lookup",
        "answer_markdown": "The latency result used workload W [citation-1].",
        "claims": [AnswerClaim(text="The latency workload was W.", citation_ids=["citation-1"])],
        "citations": [_citation()],
    }
    values.update(overrides)
    return StructuredAnswer.model_validate(values)


def _qa_record(**overrides: object) -> QaRecord:
    snapshot = _snapshot()
    values: dict[str, object] = {
        "id": "qa-1",
        "conversation_id": "conversation-1",
        "question_message_id": "message-1",
        "answer_message_id": "message-2",
        "standalone_question": "What workload was used for the latency result?",
        "normalized_question": "what workload was used for the latency result",
        "normalized_question_hash": SHA_C,
        "intent": "fact_lookup",
        "context_plan": ContextPlan(
            standalone_question="What workload was used for the latency result?",
            intent=QuestionIntent.FACT_LOOKUP,
            sources=[ContextSource.SUMMARY, ContextSource.RAW],
            paper_ids=["paper-1"],
            retrieval_queries=["evaluation latency workload"],
        ),
        "answer": _answer(),
        "source_snapshot": snapshot,
        "source_fingerprint": source_fingerprint(snapshot),
        "prompt_version": "1",
        "answer_schema_version": "1",
        "created_at": "2026-09-07 10:00:00",
    }
    values.update(overrides)
    return QaRecord.model_validate(values)


def test_qa_record_round_trips_through_json() -> None:
    record = _qa_record(archived_at="2026-09-07 11:00:00", archive_title="Latency workload")

    restored = QaRecord.model_validate_json(record.model_dump_json())

    assert restored == record


def test_answer_rejects_claims_with_unknown_citations() -> None:
    with pytest.raises(ValidationError, match="unknown citations"):
        _answer(claims=[AnswerClaim(text="Unsupported.", citation_ids=["citation-9"])])


def test_answer_rejects_duplicate_citation_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _answer(citations=[_citation("citation-1"), _citation("citation-1")])


def test_answer_allows_uncertain_response_without_claims() -> None:
    answer = _answer(claims=[], citations=[], answer_markdown="不确定：来源中没有相关实验数据。")

    assert answer.claims == []


def test_citation_requires_a_locator() -> None:
    citation = _citation().model_dump()
    citation["summary_path"] = None
    citation["page_start"] = None
    citation["page_end"] = None

    with pytest.raises(ValidationError, match="locator"):
        Citation.model_validate(citation)


def test_citation_rejects_inverted_page_range() -> None:
    with pytest.raises(ValidationError, match="page_end"):
        Citation(
            citation_id="citation-1",
            paper_id="paper-1",
            artifact_kind=CitationArtifactKind.EXTRACTED,
            artifact_sha256=SHA_A,
            section="Evaluation",
            page_start=6,
            page_end=5,
        )


def test_citation_rejects_page_end_without_start() -> None:
    with pytest.raises(ValidationError, match="page_end requires page_start"):
        Citation(
            citation_id="citation-1",
            paper_id="paper-1",
            artifact_kind=CitationArtifactKind.EXTRACTED,
            artifact_sha256=SHA_A,
            section="Evaluation",
            page_end=5,
        )


def test_citation_rejects_malformed_sha256() -> None:
    with pytest.raises(ValidationError):
        Citation(
            citation_id="citation-1",
            paper_id="paper-1",
            artifact_kind=CitationArtifactKind.EXTRACTED,
            artifact_sha256="not-a-sha256",
            section="Evaluation",
        )


def test_snapshot_rejects_scope_without_matching_payload() -> None:
    with pytest.raises(ValidationError, match="paper scope"):
        _snapshot(paper=None)
    with pytest.raises(ValidationError, match="paper scope"):
        _snapshot(collection=_collection_snapshot())


def test_collection_snapshot_requires_unique_nonempty_papers() -> None:
    with pytest.raises(ValidationError):
        _collection_snapshot(papers=[])
    with pytest.raises(ValidationError, match="unique"):
        _collection_snapshot(papers=[_paper_snapshot("paper-1"), _paper_snapshot("paper-1")])


def test_collection_scope_snapshot_round_trips() -> None:
    snapshot = _snapshot(
        scope="collection",
        paper=None,
        collection=_collection_snapshot(),
    )

    restored = SourceSnapshot.model_validate_json(snapshot.model_dump_json())

    assert restored == snapshot
    assert snapshot.paper_ids() == ("paper-1", "paper-2")


def _collection_snapshot(**overrides: object) -> CollectionSourceSnapshot:
    values: dict[str, object] = {
        "collection_id": "collection-1",
        "name": "Scheduling",
        "papers": [_paper_snapshot("paper-1"), _paper_snapshot("paper-2")],
    }
    values.update(overrides)
    return CollectionSourceSnapshot.model_validate(values)


def test_paper_snapshot_rejects_duplicate_artifact_kinds() -> None:
    with pytest.raises(ValidationError, match="unique"):
        PaperSourceSnapshot(
            paper_id="paper-1",
            status="summarized",
            artifacts=[_artifact_ref("summary_json"), _artifact_ref("summary_json", SHA_B)],
        )


def test_source_fingerprint_is_canonical_and_sensitive() -> None:
    snapshot = _snapshot()
    shuffled = snapshot.model_dump_json()
    reordered = SourceSnapshot.model_validate(
        {**snapshot.model_dump(mode="json"), "scope": "paper"}
    )

    assert source_fingerprint(reordered) == source_fingerprint(snapshot)
    assert len(shuffled) > 0

    changed = _snapshot(
        paper=_paper_snapshot().model_copy(
            update={"artifacts": [_artifact_ref("summary_json", SHA_C)]}
        )
    )
    assert source_fingerprint(changed) != source_fingerprint(snapshot)


def test_context_plan_requires_retrieval_queries_for_raw() -> None:
    with pytest.raises(ValidationError, match="retrieval query"):
        ContextPlan(
            standalone_question="What workload was used?",
            intent=QuestionIntent.FACT_LOOKUP,
            sources=[ContextSource.RAW],
        )


def test_context_plan_rejects_duplicate_sources_and_papers() -> None:
    with pytest.raises(ValidationError, match="sources must be unique"):
        ContextPlan(
            standalone_question="What workload was used?",
            intent=QuestionIntent.FACT_LOOKUP,
            sources=[ContextSource.SUMMARY, ContextSource.SUMMARY],
        )
    with pytest.raises(ValidationError, match="paper_ids must be unique"):
        ContextPlan(
            standalone_question="Compare the papers.",
            intent=QuestionIntent.COMPARISON,
            sources=[ContextSource.PAPER_SUMMARIES],
            paper_ids=["paper-1", "paper-1"],
        )


def test_context_plan_rejects_blank_question() -> None:
    with pytest.raises(ValidationError):
        ContextPlan(
            standalone_question="   ",
            intent=QuestionIntent.OVERVIEW,
            sources=[ContextSource.SUMMARY],
        )


def test_conversation_requires_exactly_one_scope_target() -> None:
    base = {
        "id": "conversation-1",
        "title": "Latency questions",
        "created_at": "2026-09-07 10:00:00",
        "updated_at": "2026-09-07 10:00:00",
    }

    with pytest.raises(ValidationError, match="paper scope"):
        Conversation.model_validate({**base, "scope": "paper"})
    with pytest.raises(ValidationError, match="collection scope"):
        Conversation.model_validate({**base, "scope": "collection", "paper_id": "paper-1"})

    conversation = Conversation.model_validate({**base, "scope": "paper", "paper_id": "paper-1"})
    assert conversation.scope.value == "paper"


def test_qa_record_rejects_fingerprint_mismatch() -> None:
    with pytest.raises(ValidationError, match="fingerprint"):
        _qa_record(source_fingerprint=SHA_B)


def test_qa_record_rejects_citations_outside_the_snapshot() -> None:
    answer = _answer(citations=[_citation(paper_id="paper-9")])

    with pytest.raises(ValidationError, match="outside the snapshot"):
        _qa_record(answer=answer)


def test_qa_record_rejects_answer_question_mismatch() -> None:
    answer = _answer(standalone_question="Another question?")

    with pytest.raises(ValidationError, match="standalone question"):
        _qa_record(answer=answer)
