from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ABSTRACT_FIX_SCHEMA_VERSION = "1"


class AbstractFixCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cleaned_abstract: str = Field(min_length=1)
    corrections: list[str] = Field(default_factory=list)


class CleanedAbstractArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    paper_id: str
    raw_abstract_sha256: str
    prompt_sha256: str
    model: str
    cleaned_abstract: str
    corrections: list[str]
