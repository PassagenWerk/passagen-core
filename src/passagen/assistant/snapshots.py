"""Build immutable paper source snapshots for conversation turns."""

from __future__ import annotations

from pathlib import Path

from passagen.assistant.errors import ScopeError
from passagen.assistant.schemas import (
    ArtifactRef,
    ConversationScope,
    PaperSourceSnapshot,
    SourceSnapshot,
)
from passagen.assistant.versions import (
    ANSWER_SCHEMA_VERSION,
    CONTEXT_BUILDER_VERSION,
    QA_PROMPT_VERSION,
    RETRIEVAL_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)
from passagen.storage.repository import get_artifact, get_paper

EXTRACTED_ARTIFACT_KIND = "extracted_json"
SUMMARY_ARTIFACT_KIND = "summary_json"
OUTLINE_ARTIFACT_KIND = "outline_md"

_REQUIRED_KINDS = (EXTRACTED_ARTIFACT_KIND, SUMMARY_ARTIFACT_KIND)
_OPTIONAL_KINDS = (OUTLINE_ARTIFACT_KIND,)


def build_paper_snapshot(database_path: Path, paper_id: str) -> SourceSnapshot:
    """Snapshot the artifacts a paper-scoped conversation turn may use.

    Question answering is summary-first with raw escalation, so both the parsed
    text and the validated summary must exist before a turn can start.
    """

    paper = get_paper(database_path, paper_id)
    if paper is None:
        raise ScopeError(f"Paper not found: {paper_id}")
    artifacts: list[ArtifactRef] = []
    for kind in (*_REQUIRED_KINDS, *_OPTIONAL_KINDS):
        artifact = get_artifact(database_path, paper_id, kind)
        if artifact is None:
            if kind in _REQUIRED_KINDS:
                raise ScopeError(
                    f"Paper {paper_id} has no {kind} artifact; "
                    "parse and summarize the paper before asking questions"
                )
            continue
        if artifact.sha256 is None or artifact.version is None:
            raise ScopeError(
                f"Paper {paper_id} has an incomplete {kind} artifact index; rebuild the paper"
            )
        artifacts.append(
            ArtifactRef(
                artifact_id=artifact.id,
                kind=kind,
                schema_version=artifact.version,
                sha256=artifact.sha256,
            )
        )
    return SourceSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        scope=ConversationScope.PAPER,
        paper=PaperSourceSnapshot(
            paper_id=paper.id,
            title=paper.title,
            status=paper.status.value,
            artifacts=artifacts,
        ),
        context_builder_version=CONTEXT_BUILDER_VERSION,
        retrieval_version=RETRIEVAL_VERSION,
        prompt_version=QA_PROMPT_VERSION,
        answer_schema_version=ANSWER_SCHEMA_VERSION,
    )
