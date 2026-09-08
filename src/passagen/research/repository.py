from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from passagen.research.schemas import CollectionArtifact
from passagen.storage.engine import session_scope
from passagen.storage.models import CollectionArtifactRow, GenerationRunRow


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    kind: str
    path: str
    sha256: str
    size_bytes: int


def save_synthesis_artifacts(
    database_path: Path,
    *,
    collection_id: str,
    run_id: str,
    version: str,
    source_fingerprint: str,
    artifacts: tuple[ArtifactWrite, ...],
) -> tuple[CollectionArtifact, ...]:
    """Index the complete immutable bundle and finish its run in one transaction."""

    with session_scope(database_path) as session:
        rows = [
            CollectionArtifactRow(
                id=str(uuid.uuid4()),
                collection_id=collection_id,
                generation_run_id=run_id,
                kind=artifact.kind,
                path=artifact.path,
                version=version,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                source_fingerprint=source_fingerprint,
            )
            for artifact in artifacts
        ]
        session.add_all(rows)
        run = session.get(GenerationRunRow, run_id)
        if run is None:
            raise KeyError(f"Generation run not found: {run_id}")
        run.status = "completed"
        run.completed_at = _now(session)
        session.flush()
        return tuple(_artifact(row) for row in rows)


def latest_synthesis_artifacts(
    database_path: Path,
    collection_id: str,
    *,
    source_fingerprint: str | None = None,
) -> tuple[CollectionArtifact, ...]:
    with session_scope(database_path) as session:
        statement = select(CollectionArtifactRow).where(
            CollectionArtifactRow.collection_id == collection_id,
            CollectionArtifactRow.kind == "synthesis_json",
        )
        if source_fingerprint is not None:
            statement = statement.where(
                CollectionArtifactRow.source_fingerprint == source_fingerprint
            )
        json_row = session.scalar(
            statement.order_by(literal_column("collection_artifacts.rowid").desc()).limit(1)
        )
        if json_row is None or json_row.generation_run_id is None:
            return ()
        rows = session.scalars(
            select(CollectionArtifactRow)
            .where(CollectionArtifactRow.generation_run_id == json_row.generation_run_id)
            .order_by(CollectionArtifactRow.kind)
        ).all()
        return tuple(_artifact(row) for row in rows)


def fail_synthesis_run(
    database_path: Path, run_id: str, *, error_code: str, error_message: str
) -> None:
    with session_scope(database_path) as session:
        run = session.get(GenerationRunRow, run_id)
        if run is not None:
            run.status = "failed"
            run.error_code = error_code
            run.error_message = error_message
            run.completed_at = _now(session)


def _artifact(row: CollectionArtifactRow) -> CollectionArtifact:
    return CollectionArtifact(
        id=row.id,
        collection_id=row.collection_id,
        generation_run_id=row.generation_run_id,
        kind=row.kind,
        path=row.path,
        version=row.version,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        source_fingerprint=row.source_fingerprint,
        created_at=row.created_at,
    )


def _now(session: Session) -> str:
    from sqlalchemy import func

    return str(session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))))
