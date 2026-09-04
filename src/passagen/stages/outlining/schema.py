from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

OUTLINE_SCHEMA_VERSION = "2"
_SECTIONS = (
    ("introduction", "Introduction"),
    ("background", "Background and Motivation"),
    ("design", "Design"),
    ("implementation", "Implementation"),
    ("evaluation", "Evaluation"),
    ("limitations", "Limitations and Trade-offs"),
    ("related_work", "Related Work"),
    ("conclusion", "Conclusion"),
)
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class OutlinePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: NonEmptyText = Field(description="A specific named subtopic within the section.")
    details: list[NonEmptyText] = Field(
        default_factory=list,
        description="Supporting technical details grounded in the validated summary.",
    )
    evidence_pages: list[int] = Field(
        default_factory=list,
        description="Evidence pages copied from the summary when available.",
    )


class OutlineSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: NonEmptyText | None = Field(
        default=None, description="A concise section-level thesis grounded in the summary."
    )
    points: list[OutlinePoint] = Field(
        default_factory=list, description="Named subtopics and supporting details."
    )

    @property
    def has_content(self) -> bool:
        return self.thesis is not None or bool(self.points)


class PaperOutline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    introduction: OutlineSection = Field(default_factory=OutlineSection)
    background: OutlineSection = Field(default_factory=OutlineSection)
    design: OutlineSection = Field(default_factory=OutlineSection)
    implementation: OutlineSection = Field(default_factory=OutlineSection)
    evaluation: OutlineSection = Field(default_factory=OutlineSection)
    limitations: OutlineSection = Field(default_factory=OutlineSection)
    related_work: OutlineSection = Field(default_factory=OutlineSection)
    conclusion: OutlineSection = Field(default_factory=OutlineSection)

    @model_validator(mode="after")
    def require_content(self) -> "PaperOutline":
        if not any(getattr(self, field).has_content for field, _heading in _SECTIONS):
            raise ValueError("outline must contain at least one non-empty section")
        return self
