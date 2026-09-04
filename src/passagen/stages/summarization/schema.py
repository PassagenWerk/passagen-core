from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SUMMARY_SCHEMA_VERSION: Literal["2"] = "2"


EVIDENCE_CATEGORIES: tuple[str, ...] = (
    "problem",
    "motivation",
    "goal",
    "non_goal",
    "assumption",
    "prior_work_limitation",
    "contribution",
    "design_component",
    "process",
    "mechanism",
    "implementation_detail",
    "evaluation_setup",
    "evaluation_result",
    "ablation",
    "limitation",
    "tradeoff",
    "threat_to_validity",
    "conclusion",
    "related_work_distinction",
)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(
        description=f"One of: {', '.join(EVIDENCE_CATEGORIES)}.",
    )
    claim: str = Field(description="Concise evidence-backed English statement.")
    section: str | None = Field(
        default=None, description="Section title or path the claim comes from."
    )
    evidence_pages: list[int] = Field(default_factory=list)
    subject: str | None = Field(
        default=None, description="Evaluated subject for quantitative results."
    )
    subject_value: str | None = Field(
        default=None, description="Measured value belonging to the subject."
    )
    baseline: str | None = None
    baseline_value: str | None = Field(
        default=None, description="Measured value belonging to the named baseline."
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="Workload, dataset, configuration, or other measurement conditions.",
    )
    source_excerpt: str | None = Field(
        default=None, description="Short quote from the source supporting the claim."
    )


class ExtractedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence: list[EvidenceItem] = Field(default_factory=list)


class SummaryIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None


class PaperClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_type: str | None = Field(
        default=None,
        description=(
            "General paper type, such as system, architecture, compiler, algorithm, "
            "measurement, or empirical study."
        ),
    )
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class SummaryProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str | None = Field(
        default=None, description="Technical context needed to understand the problem."
    )
    problem_statement: str | None = Field(
        default=None, description="The specific research problem addressed by the paper."
    )
    motivation: str | None = Field(
        default=None, description="Why solving the stated problem matters."
    )
    goals: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    prior_work_limitations: list[str] = Field(
        default_factory=list,
        description="Concrete limitations of prior approaches stated by the paper.",
    )


class Contribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = Field(
        default=None,
        description=(
            "Contribution category, such as design, mechanism, implementation, analysis, "
            "benchmark, measurement, or methodology."
        ),
    )
    statement: str
    evidence_pages: list[int] = Field(default_factory=list)


class DesignComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str | None = None
    details: list[str] = Field(default_factory=list)
    interactions: list[str] = Field(default_factory=list)
    evidence_pages: list[int] = Field(default_factory=list)


class DesignProcess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    steps: list[str] = Field(default_factory=list)
    evidence_pages: list[int] = Field(default_factory=list)


class SummaryDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overview: str | None = Field(
        default=None, description="High-level design and how it addresses the stated problem."
    )
    components: list[DesignComponent] = Field(default_factory=list)
    processes: list[DesignProcess] = Field(default_factory=list)
    key_mechanisms: list[str] = Field(default_factory=list)
    design_decisions: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)


class SummaryImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prototype_scope: str | None = None
    implemented_components: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    frameworks_and_dependencies: list[str] = Field(
        default_factory=list,
        description="Frameworks, libraries, toolchains, and external dependencies.",
    )
    hardware_platforms: list[str] = Field(default_factory=list)
    software_platforms: list[str] = Field(default_factory=list)
    code_size: str | None = None
    deployment_model: str | None = None
    engineering_details: list[str] = Field(default_factory=list)


class EvaluationEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hardware: list[str] = Field(default_factory=list)
    software: list[str] = Field(default_factory=list)
    topology_or_scale: str | None = None
    configuration: list[str] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_question: str | None = None
    metric: str
    metric_direction: Literal["higher_is_better", "lower_is_better", "neutral", "unknown"] = (
        "unknown"
    )
    subject: str
    subject_value: str | None = Field(
        default=None, description="Measured value belonging to the evaluated subject."
    )
    baseline: str | None = None
    baseline_value: str | None = Field(
        default=None, description="Measured value belonging to the named baseline."
    )
    improvement: str | None = None
    conditions: list[str] = Field(default_factory=list)
    evidence_pages: list[int] = Field(
        min_length=1, description="Source pages supporting the complete result."
    )


class SummaryEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_questions: list[str] = Field(default_factory=list)
    environment: EvaluationEnvironment = Field(default_factory=EvaluationEnvironment)
    baselines: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(
        default_factory=list, description="Datasets used in the evaluation."
    )
    workloads: list[str] = Field(
        default_factory=list, description="Benchmarks, applications, or workloads evaluated."
    )
    metrics: list[str] = Field(default_factory=list)
    methodology: list[str] = Field(default_factory=list)
    results: list[EvaluationResult] = Field(default_factory=list)
    ablations: list[EvaluationResult] = Field(default_factory=list)


class SummaryDiscussion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limitations: list[str] = Field(
        default_factory=list, description="Limitations and trade-offs acknowledged by the paper."
    )
    tradeoffs: list[str] = Field(default_factory=list)
    threats_to_validity: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    future_work: list[str] = Field(default_factory=list)
    conclusions: list[str] = Field(
        default_factory=list, description="Conclusions directly supported by the paper."
    )
    reusable_methods: list[str] = Field(
        default_factory=list, description="Methods that could be reused in related research."
    )


class RelatedWorkGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: str
    representative_works: list[str] = Field(default_factory=list)
    relationship: str | None = None
    distinction: str | None = None
    evidence_pages: list[int] = Field(default_factory=list)


class SummaryRelatedWork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[RelatedWorkGroup] = Field(default_factory=list)


class StructuredSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["2"] = SUMMARY_SCHEMA_VERSION
    identity: SummaryIdentity
    classification: PaperClassification = Field(default_factory=PaperClassification)
    problem: SummaryProblem = Field(default_factory=SummaryProblem)
    contributions: list[Contribution] = Field(default_factory=list)
    design: SummaryDesign = Field(default_factory=SummaryDesign)
    implementation: SummaryImplementation = Field(default_factory=SummaryImplementation)
    evaluation: SummaryEvaluation = Field(default_factory=SummaryEvaluation)
    discussion: SummaryDiscussion = Field(default_factory=SummaryDiscussion)
    related_work: SummaryRelatedWork = Field(default_factory=SummaryRelatedWork)
