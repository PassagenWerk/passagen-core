from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from passagen.assistant.schemas import AnswerClaim, Citation, NonBlankStr, Sha256Hex, SourceStatus
from passagen.assistant.versions import COLLECTION_SYNTHESIS_SCHEMA_VERSION, REPORT_SCHEMA_VERSION


class SynthesisTheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonBlankStr
    description: NonBlankStr
    paper_ids: list[NonBlankStr] = Field(min_length=1)
    citation_ids: list[NonBlankStr] = Field(min_length=1)


class SynthesisPaperRole(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: NonBlankStr
    role: NonBlankStr
    contribution: NonBlankStr
    method: NonBlankStr | None = None
    citation_ids: list[NonBlankStr] = Field(min_length=1)


class SynthesisInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonBlankStr
    description: NonBlankStr
    paper_ids: list[NonBlankStr] = Field(min_length=1)
    citation_ids: list[NonBlankStr] = Field(min_length=1)


class SynthesisOpenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: NonBlankStr
    rationale: NonBlankStr
    paper_ids: list[NonBlankStr] = Field(min_length=1)
    citation_ids: list[NonBlankStr] = Field(min_length=1)


class ComparisonCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: NonBlankStr
    value: NonBlankStr
    citation_ids: list[NonBlankStr] = Field(min_length=1)


class ComparisonRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: NonBlankStr
    cells: list[ComparisonCell] = Field(default_factory=list)


class ComparisonMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimensions: list[NonBlankStr] = Field(default_factory=list)
    rows: list[ComparisonRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _is_rectangular(self) -> Self:
        if len(self.dimensions) != len(set(self.dimensions)):
            raise ValueError("comparison dimensions must be unique")
        expected = set(self.dimensions)
        paper_ids: set[str] = set()
        for row in self.rows:
            if row.paper_id in paper_ids:
                raise ValueError("comparison rows must have unique paper ids")
            paper_ids.add(row.paper_id)
            dimensions = [cell.dimension for cell in row.cells]
            if len(dimensions) != len(set(dimensions)) or set(dimensions) != expected:
                raise ValueError("each comparison row must contain exactly one cell per dimension")
        return self


class SynthesisCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    included_paper_ids: list[NonBlankStr] = Field(min_length=1)
    missing_summary_paper_ids: list[NonBlankStr] = Field(default_factory=list)
    partial: bool = False

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        included = set(self.included_paper_ids)
        missing = set(self.missing_summary_paper_ids)
        if len(included) != len(self.included_paper_ids):
            raise ValueError("included paper ids must be unique")
        if len(missing) != len(self.missing_summary_paper_ids):
            raise ValueError("missing-summary paper ids must be unique")
        if included & missing:
            raise ValueError("included and missing-summary paper ids must not overlap")
        if self.partial != bool(missing):
            raise ValueError("partial must reflect missing summary coverage")
        return self


class CollectionSynthesisV1(BaseModel):
    """Persisted v1 synthesis retained for library compatibility."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    overview: NonBlankStr
    themes: list[SynthesisTheme] = Field(default_factory=list)
    comparison_matrix: ComparisonMatrix = Field(default_factory=ComparisonMatrix)
    claims: list[AnswerClaim] = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)
    coverage: SynthesisCoverage

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("citation ids must be unique within a synthesis")
        known = set(citation_ids)
        references = [citation_id for theme in self.themes for citation_id in theme.citation_ids]
        references.extend(
            citation_id
            for row in self.comparison_matrix.rows
            for cell in row.cells
            for citation_id in cell.citation_ids
        )
        references.extend(
            citation_id for claim in self.claims for citation_id in claim.citation_ids
        )
        missing = sorted(set(references) - known)
        if missing:
            raise ValueError(f"synthesis references unknown citations: {', '.join(missing)}")
        return self


class CollectionSynthesis(BaseModel):
    """Current provider-independent collection intelligence artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = COLLECTION_SYNTHESIS_SCHEMA_VERSION
    executive_overview: NonBlankStr
    paper_roles: list[SynthesisPaperRole] = Field(default_factory=list)
    themes: list[SynthesisTheme] = Field(default_factory=list)
    comparison_matrix: ComparisonMatrix = Field(default_factory=ComparisonMatrix)
    agreements: list[SynthesisInsight] = Field(default_factory=list)
    disagreements: list[SynthesisInsight] = Field(default_factory=list)
    complementary_contributions: list[SynthesisInsight] = Field(default_factory=list)
    gaps: list[SynthesisInsight] = Field(default_factory=list)
    open_questions: list[SynthesisOpenQuestion] = Field(default_factory=list)
    claims: list[AnswerClaim] = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)
    coverage: SynthesisCoverage

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("citation ids must be unique within a synthesis")
        role_papers = [role.paper_id for role in self.paper_roles]
        if len(role_papers) != len(set(role_papers)):
            raise ValueError("paper roles must have unique paper ids")
        known = set(citation_ids)
        references = [citation_id for role in self.paper_roles for citation_id in role.citation_ids]
        references.extend(
            citation_id for theme in self.themes for citation_id in theme.citation_ids
        )
        references.extend(
            citation_id
            for row in self.comparison_matrix.rows
            for cell in row.cells
            for citation_id in cell.citation_ids
        )
        references.extend(
            citation_id
            for insights in (
                self.agreements,
                self.disagreements,
                self.complementary_contributions,
                self.gaps,
            )
            for insight in insights
            for citation_id in insight.citation_ids
        )
        references.extend(
            citation_id for question in self.open_questions for citation_id in question.citation_ids
        )
        references.extend(
            citation_id for claim in self.claims for citation_id in claim.citation_ids
        )
        missing = sorted(set(references) - known)
        if missing:
            raise ValueError(f"synthesis references unknown citations: {', '.join(missing)}")
        return self


def upgrade_synthesis_v1(synthesis: CollectionSynthesisV1) -> CollectionSynthesis:
    """Project a persisted v1 artifact into the current read model without inventing content."""

    return CollectionSynthesis(
        executive_overview=synthesis.overview,
        themes=synthesis.themes,
        comparison_matrix=synthesis.comparison_matrix,
        claims=synthesis.claims,
        citations=synthesis.citations,
        coverage=synthesis.coverage,
    )


class CollectionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    collection_id: NonBlankStr
    generation_run_id: NonBlankStr | None = None
    kind: NonBlankStr
    path: NonBlankStr
    version: NonBlankStr
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    source_fingerprint: Sha256Hex
    created_at: NonBlankStr


class CollectionSynthesisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    synthesis: CollectionSynthesis
    run_id: NonBlankStr | None
    artifacts: list[CollectionArtifact]
    source_status: SourceStatus
    disposition: Literal["generated", "reused"]
    strategy: Literal["direct", "map_reduce", "reused"]


class ReportKind(StrEnum):
    REVIEW = "review"
    COMPARISON = "comparison"
    GAPS = "gaps"
    CUSTOM = "custom"


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: NonBlankStr
    body_markdown: NonBlankStr
    claims: list[AnswerClaim] = Field(default_factory=list)


class CollectionReport(BaseModel):
    """Versioned, citation-checked collection report artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = REPORT_SCHEMA_VERSION
    kind: ReportKind
    title: NonBlankStr
    user_prompt: str | None = None
    synthesis_artifact_id: NonBlankStr | None = None
    coverage: SynthesisCoverage
    sections: list[ReportSection] = Field(min_length=1)
    claims: list[AnswerClaim] = Field(default_factory=list)
    citations: list[Citation] = Field(min_length=1)

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("citation ids must be unique within a report")
        known = set(citation_ids)
        references = [citation_id for claim in self.claims for citation_id in claim.citation_ids]
        references.extend(
            citation_id
            for section in self.sections
            for claim in section.claims
            for citation_id in claim.citation_ids
        )
        missing = sorted(set(references) - known)
        if missing:
            raise ValueError(f"report references unknown citations: {', '.join(missing)}")
        return self


class CollectionReportRecord(BaseModel):
    """Persisted lifecycle view of one collection report."""

    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    collection_id: NonBlankStr
    kind: ReportKind
    status: Literal["queued", "running", "completed", "failed"]
    title: NonBlankStr
    user_prompt: NonBlankStr | None = None
    source_snapshot_json: NonBlankStr
    source_fingerprint: Sha256Hex
    run_id: NonBlankStr | None = None
    report_artifact_id: NonBlankStr | None = None
    error: str | None = None
    created_at: NonBlankStr
    completed_at: NonBlankStr | None = None


class CollectionReportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: CollectionReportRecord
    report: CollectionReport
    artifacts: list[CollectionArtifact]
    source_status: SourceStatus
    disposition: Literal["generated", "reused"]


class CollectionReportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: CollectionReportRecord
    report: CollectionReport | None
    artifacts: list[CollectionArtifact]
    source_status: SourceStatus
