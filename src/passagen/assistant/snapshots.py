"""Build immutable paper source snapshots for conversation turns."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import literal_column, select

from passagen.assistant.errors import ScopeError
from passagen.assistant.schemas import (
    ArtifactRef,
    CollectionSourceSnapshot,
    ConversationScope,
    PaperSourceSnapshot,
    SourceSnapshot,
    source_fingerprint,
)
from passagen.assistant.versions import (
    ANSWER_SCHEMA_VERSION,
    COLLECTION_SYNTHESIS_PROMPT_VERSION,
    COLLECTION_SYNTHESIS_SCHEMA_VERSION,
    CONTEXT_BUILDER_VERSION,
    QA_PROMPT_VERSION,
    RETRIEVAL_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)
from passagen.catalog import CatalogNotFoundError, CatalogService
from passagen.storage.engine import session_scope
from passagen.storage.models import CollectionArtifactRow
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


def build_collection_snapshot(
    database_path: Path, data_dir: Path, collection_id: str
) -> SourceSnapshot:
    """Capture ordered collection membership and each current Summary artifact."""

    try:
        collection = CatalogService(database_path, data_dir).get_collection(collection_id)
    except CatalogNotFoundError as exc:
        raise ScopeError(f"Collection not found: {collection_id}") from exc
    if not collection.papers:
        raise ScopeError(f"Collection {collection_id} is empty")
    papers: list[PaperSourceSnapshot] = []
    for membership in collection.papers:
        paper = get_paper(database_path, membership.paper_id)
        if paper is None:
            raise ScopeError(f"Collection paper not found: {membership.paper_id}")
        summary = get_artifact(database_path, paper.id, SUMMARY_ARTIFACT_KIND)
        artifacts = []
        if summary is not None and summary.sha256 is not None and summary.version is not None:
            artifacts.append(
                ArtifactRef(
                    artifact_id=summary.id,
                    kind=SUMMARY_ARTIFACT_KIND,
                    schema_version=summary.version,
                    sha256=summary.sha256,
                )
            )
        papers.append(
            PaperSourceSnapshot(
                paper_id=paper.id,
                title=paper.title,
                status=paper.status.value,
                artifacts=artifacts,
            )
        )
    return SourceSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        scope=ConversationScope.COLLECTION,
        collection=CollectionSourceSnapshot(
            collection_id=collection.id,
            name=collection.name,
            description=collection.description,
            papers=papers,
        ),
        context_builder_version=CONTEXT_BUILDER_VERSION,
        retrieval_version=RETRIEVAL_VERSION,
        prompt_version=COLLECTION_SYNTHESIS_PROMPT_VERSION,
        answer_schema_version=COLLECTION_SYNTHESIS_SCHEMA_VERSION,
    )


def build_collection_qa_snapshot(
    database_path: Path, data_dir: Path, collection_id: str
) -> SourceSnapshot:
    """Snapshot every source a collection-scoped answer may use.

    Unlike the synthesis snapshot, each member paper contributes every usable
    artifact (Summary, extracted text, Outline), and the latest collection
    synthesis is referenced when its fingerprint still matches the current
    membership and summaries.
    """

    try:
        collection = CatalogService(database_path, data_dir).get_collection(collection_id)
    except CatalogNotFoundError as exc:
        raise ScopeError(f"Collection not found: {collection_id}") from exc
    if not collection.papers:
        raise ScopeError(f"Collection {collection_id} is empty")
    papers: list[PaperSourceSnapshot] = []
    for membership in collection.papers:
        paper = get_paper(database_path, membership.paper_id)
        if paper is None:
            raise ScopeError(f"Collection paper not found: {membership.paper_id}")
        artifacts: list[ArtifactRef] = []
        for kind in (SUMMARY_ARTIFACT_KIND, EXTRACTED_ARTIFACT_KIND, OUTLINE_ARTIFACT_KIND):
            artifact = get_artifact(database_path, paper.id, kind)
            if artifact is None:
                continue
            if artifact.sha256 is None or artifact.version is None:
                raise ScopeError(
                    f"Paper {paper.id} has an incomplete {kind} artifact index; rebuild the paper"
                )
            artifacts.append(
                ArtifactRef(
                    artifact_id=artifact.id,
                    kind=kind,
                    schema_version=artifact.version,
                    sha256=artifact.sha256,
                )
            )
        kinds = {artifact.kind for artifact in artifacts}
        if not kinds & {SUMMARY_ARTIFACT_KIND, EXTRACTED_ARTIFACT_KIND}:
            raise ScopeError(
                f"Collection paper {paper.id} has no summary_json or extracted_json artifact; "
                "process the paper before asking collection questions"
            )
        papers.append(
            PaperSourceSnapshot(
                paper_id=paper.id,
                title=paper.title,
                status=paper.status.value,
                artifacts=artifacts,
            )
        )
    snapshot = SourceSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        scope=ConversationScope.COLLECTION,
        collection=CollectionSourceSnapshot(
            collection_id=collection.id,
            name=collection.name,
            description=collection.description,
            papers=papers,
        ),
        context_builder_version=CONTEXT_BUILDER_VERSION,
        retrieval_version=RETRIEVAL_VERSION,
        prompt_version=QA_PROMPT_VERSION,
        answer_schema_version=ANSWER_SCHEMA_VERSION,
    )
    # A synthesis is reusable for QA exactly when it was built from the current
    # membership and summaries, i.e. its fingerprint matches the synthesis
    # scope snapshot (which only covers summary artifacts).
    synthesis_fingerprint = source_fingerprint(
        build_collection_snapshot(database_path, data_dir, collection.id)
    )
    synthesis = _matching_synthesis_ref(database_path, collection.id, synthesis_fingerprint)
    if synthesis is not None and snapshot.collection is not None:
        snapshot = snapshot.model_copy(
            update={"collection": snapshot.collection.model_copy(update={"synthesis": synthesis})}
        )
    return snapshot


def _matching_synthesis_ref(
    database_path: Path, collection_id: str, base_fingerprint: str
) -> ArtifactRef | None:
    """Reference the newest synthesis built from the same membership and summaries."""

    with session_scope(database_path) as session:
        row = session.scalar(
            select(CollectionArtifactRow)
            .where(
                CollectionArtifactRow.collection_id == collection_id,
                CollectionArtifactRow.kind == "synthesis_json",
                CollectionArtifactRow.source_fingerprint == base_fingerprint,
            )
            .order_by(literal_column("collection_artifacts.rowid").desc())
            .limit(1)
        )
    if row is None:
        return None
    return ArtifactRef(
        artifact_id=row.id,
        kind="synthesis_json",
        schema_version=row.version,
        sha256=row.sha256,
    )


def load_synthesis_text(database_path: Path, data_dir: Path, artifact: ArtifactRef) -> str:
    """Load the referenced synthesis JSON, verifying its indexed content hash."""

    with session_scope(database_path) as session:
        row = session.get(CollectionArtifactRow, artifact.artifact_id)
    if row is None or row.kind != "synthesis_json":
        raise ScopeError(f"Collection synthesis artifact not found: {artifact.artifact_id}")
    try:
        content = (data_dir / row.path).read_bytes()
    except OSError as exc:
        raise ScopeError(f"Cannot load collection synthesis: {exc}") from exc
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise ScopeError("Stored collection synthesis hash does not match the source snapshot")
    return content.decode("utf-8")
