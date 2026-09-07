from __future__ import annotations

import pytest

from passagen.assistant.citations import (
    PaperEvidenceIndex,
    validate_answer_citations,
)
from passagen.assistant.errors import CitationValidationError
from passagen.assistant.schemas import (
    AnswerClaim,
    ArtifactRef,
    Citation,
    CitationArtifactKind,
    PaperSourceSnapshot,
    QuestionIntent,
    StructuredAnswer,
)
from passagen.parsing import ParsedPaper, ParsedSection
from passagen.stages.summarization.schema import (
    EvaluationResult,
    StructuredSummary,
    SummaryEvaluation,
    SummaryIdentity,
)

SHA_SUMMARY = "a" * 64
SHA_OUTLINE = "b" * 64
SHA_EXTRACTED = "c" * 64


def _snapshot() -> PaperSourceSnapshot:
    return PaperSourceSnapshot(
        paper_id="paper-1",
        status="outlined",
        artifacts=[
            ArtifactRef(
                artifact_id="art-summary",
                kind="summary_json",
                schema_version="2",
                sha256=SHA_SUMMARY,
            ),
            ArtifactRef(
                artifact_id="art-outline",
                kind="outline_md",
                schema_version="1",
                sha256=SHA_OUTLINE,
            ),
            ArtifactRef(
                artifact_id="art-extracted",
                kind="extracted_json",
                schema_version="1",
                sha256=SHA_EXTRACTED,
            ),
        ],
    )


def _summary() -> StructuredSummary:
    return StructuredSummary(
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


def _outline() -> str:
    return "# 1 Introduction\n# 4 Evaluation\n## 4.2 Latency\n"


def _parsed() -> ParsedPaper:
    return ParsedPaper(
        sections=(
            ParsedSection(
                title="4.2 Latency Evaluation",
                text="We measure tail latency. Latency dropped to 12 ms.",
                pages=(5, 6),
            ),
        ),
        parser="fake",
    )


def _index() -> PaperEvidenceIndex:
    return PaperEvidenceIndex(
        snapshot=_snapshot(), summary=_summary(), outline=_outline(), parsed=_parsed()
    )


def _answer(citation: Citation) -> StructuredAnswer:
    return StructuredAnswer(
        standalone_question="q",
        intent=QuestionIntent.FACT_LOOKUP,
        answer_markdown="m [c-1]",
        claims=[AnswerClaim(text="claim", citation_ids=["c-1"])],
        citations=[citation],
    )


def _summary_citation(**overrides: object) -> Citation:
    values: dict[str, object] = {
        "citation_id": "c-1",
        "paper_id": "paper-1",
        "artifact_kind": "summary_json",
        "artifact_id": "art-summary",
        "artifact_sha256": SHA_SUMMARY,
        "summary_path": "evaluation.results[0]",
        "page_start": 5,
    }
    values.update(overrides)
    return Citation.model_validate(values)


def test_valid_summary_citation_passes() -> None:
    validate_answer_citations(_answer(_summary_citation()), _index())


def test_citation_from_another_paper_is_rejected() -> None:
    with pytest.raises(CitationValidationError, match="outside the source snapshot"):
        validate_answer_citations(_answer(_summary_citation(paper_id="paper-9")), _index())


def test_mismatched_artifact_hash_is_rejected() -> None:
    with pytest.raises(CitationValidationError, match="hash"):
        validate_answer_citations(_answer(_summary_citation(artifact_sha256="d" * 64)), _index())


def test_mismatched_artifact_id_is_rejected() -> None:
    with pytest.raises(CitationValidationError, match="artifact_id"):
        validate_answer_citations(_answer(_summary_citation(artifact_id="art-other")), _index())


def test_unresolvable_summary_path_is_rejected() -> None:
    with pytest.raises(CitationValidationError, match="does not resolve"):
        validate_answer_citations(
            _answer(_summary_citation(summary_path="evaluation.results[3]")), _index()
        )


def test_page_outside_evidence_pages_is_rejected() -> None:
    with pytest.raises(CitationValidationError, match="allowed evidence pages are \\[5\\]") as exc:
        validate_answer_citations(_answer(_summary_citation(page_start=6)), _index())
    assert '"metric": "latency"' in str(exc.value)
    assert "Recheck that the summary_path supports the claim" in str(exc.value)


def test_outline_section_must_match_a_heading() -> None:
    outline_citation = Citation(
        citation_id="c-1",
        paper_id="paper-1",
        artifact_kind=CitationArtifactKind.OUTLINE,
        artifact_id="art-outline",
        artifact_sha256=SHA_OUTLINE,
        section="4 Evaluation",
    )
    validate_answer_citations(_answer(outline_citation), _index())

    bad = outline_citation.model_copy(update={"section": "9 Appendix"})
    with pytest.raises(CitationValidationError, match="outline heading"):
        validate_answer_citations(_answer(bad), _index())


def test_raw_citation_checks_section_pages_and_excerpt() -> None:
    raw_citation = Citation(
        citation_id="c-1",
        paper_id="paper-1",
        artifact_kind=CitationArtifactKind.EXTRACTED,
        artifact_id="art-extracted",
        artifact_sha256=SHA_EXTRACTED,
        section="4.2 Latency Evaluation",
        page_start=5,
        page_end=6,
        excerpt="Latency dropped to 12 ms.",
    )
    validate_answer_citations(_answer(raw_citation), _index())

    bad_pages = raw_citation.model_copy(update={"page_start": 9, "page_end": 9})
    with pytest.raises(CitationValidationError, match="do not overlap"):
        validate_answer_citations(_answer(bad_pages), _index())

    bad_excerpt = raw_citation.model_copy(update={"excerpt": "invented sentence"})
    with pytest.raises(CitationValidationError, match="contiguous verbatim substring"):
        validate_answer_citations(_answer(bad_excerpt), _index())

    bad_section = raw_citation.model_copy(update={"section": "7 Conclusion"})
    with pytest.raises(CitationValidationError, match="parsed section"):
        validate_answer_citations(_answer(bad_section), _index())


def test_excerpt_matching_ignores_whitespace_and_case() -> None:
    raw_citation = Citation(
        citation_id="c-1",
        paper_id="paper-1",
        artifact_kind=CitationArtifactKind.EXTRACTED,
        artifact_id="art-extracted",
        artifact_sha256=SHA_EXTRACTED,
        section="latency evaluation",
        excerpt="latency   dropped\nto 12 ms.",
    )
    validate_answer_citations(_answer(raw_citation), _index())


def test_artifact_kind_outside_snapshot_is_rejected() -> None:
    snapshot = PaperSourceSnapshot(
        paper_id="paper-1",
        status="summarized",
        artifacts=[
            ArtifactRef(
                artifact_id="art-summary",
                kind="summary_json",
                schema_version="2",
                sha256=SHA_SUMMARY,
            )
        ],
    )
    index = PaperEvidenceIndex(snapshot=snapshot, summary=_summary(), outline=None, parsed=None)
    citation = Citation(
        citation_id="c-1",
        paper_id="paper-1",
        artifact_kind=CitationArtifactKind.EXTRACTED,
        artifact_id="art-extracted",
        artifact_sha256=SHA_EXTRACTED,
        section="4.2",
    )

    with pytest.raises(CitationValidationError, match="not part of the source snapshot"):
        validate_answer_citations(_answer(citation), index)
