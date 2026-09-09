from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import literal_column, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from passagen.research.schemas import (
    CollectionArtifact,
    CollectionReportRecord,
    ReportKind,
)
from passagen.storage.engine import session_scope
from passagen.storage.models import (
    CollectionArtifactRow,
    CollectionReportRow,
    GenerationRunRow,
)


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


def fail_generation_run(
    database_path: Path, run_id: str, *, error_code: str, error_message: str
) -> None:
    with session_scope(database_path) as session:
        run = session.get(GenerationRunRow, run_id)
        if run is not None:
            run.status = "failed"
            run.error_code = error_code
            run.error_message = error_message
            run.completed_at = _now(session)


def fail_synthesis_run(
    database_path: Path, run_id: str, *, error_code: str, error_message: str
) -> None:
    fail_generation_run(database_path, run_id, error_code=error_code, error_message=error_message)


def create_report_submission(
    database_path: Path,
    *,
    collection_id: str,
    kind: ReportKind,
    title: str,
    user_prompt: str | None,
    source_snapshot_json: str,
    source_fingerprint: str,
    reuse_policy: str,
) -> tuple[str, str]:
    """Atomically persist a queued report row and its generation run."""

    report_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    with session_scope(database_path) as session:
        session.add(
            GenerationRunRow(
                id=run_id,
                kind="report",
                collection_id=collection_id,
                status="queued",
                reuse_policy=reuse_policy,
                source_snapshot_json=source_snapshot_json,
            )
        )
        session.flush()
        session.add(
            CollectionReportRow(
                id=report_id,
                collection_id=collection_id,
                kind=kind.value,
                status="queued",
                title=title,
                user_prompt=user_prompt,
                source_snapshot_json=source_snapshot_json,
                source_fingerprint=source_fingerprint,
                run_id=run_id,
            )
        )
    return report_id, run_id


def start_report(database_path: Path, report_id: str) -> None:
    with session_scope(database_path) as session:
        row = session.get(CollectionReportRow, report_id)
        if row is not None and row.status == "queued":
            row.status = "running"


def get_report(database_path: Path, report_id: str) -> CollectionReportRecord | None:
    with session_scope(database_path) as session:
        row = session.get(CollectionReportRow, report_id)
        return _report(row) if row is not None else None


def get_report_by_run(database_path: Path, run_id: str) -> CollectionReportRecord | None:
    with session_scope(database_path) as session:
        row = session.scalar(
            select(CollectionReportRow).where(CollectionReportRow.run_id == run_id)
        )
        return _report(row) if row is not None else None


def list_reports(database_path: Path, collection_id: str) -> tuple[CollectionReportRecord, ...]:
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(CollectionReportRow)
            .where(CollectionReportRow.collection_id == collection_id)
            .order_by(CollectionReportRow.created_at.desc(), CollectionReportRow.id)
        ).all()
        return tuple(_report(row) for row in rows)


def find_reusable_report(
    database_path: Path,
    *,
    collection_id: str,
    kind: ReportKind,
    user_prompt: str | None,
    source_fingerprint: str,
) -> CollectionReportRecord | None:
    with session_scope(database_path) as session:
        statement = select(CollectionReportRow).where(
            CollectionReportRow.collection_id == collection_id,
            CollectionReportRow.kind == kind.value,
            CollectionReportRow.status == "completed",
            CollectionReportRow.source_fingerprint == source_fingerprint,
        )
        if user_prompt is None:
            statement = statement.where(CollectionReportRow.user_prompt.is_(None))
        else:
            statement = statement.where(CollectionReportRow.user_prompt == user_prompt)
        row = session.scalar(
            statement.order_by(CollectionReportRow.created_at.desc(), CollectionReportRow.id)
        )
        return _report(row) if row is not None else None


def report_artifacts(
    database_path: Path, record: CollectionReportRecord
) -> tuple[CollectionArtifact, ...]:
    if record.run_id is None:
        return ()
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(CollectionArtifactRow)
            .where(CollectionArtifactRow.generation_run_id == record.run_id)
            .order_by(CollectionArtifactRow.kind)
        ).all()
        return tuple(_artifact(row) for row in rows)


def delete_report(database_path: Path, report_id: str) -> bool:
    """Delete a terminal report and its artifact and generation records."""

    with session_scope(database_path) as session:
        report = session.get(CollectionReportRow, report_id)
        if report is None:
            return False
        run_id = report.run_id
        report.report_artifact_id = None
        report.run_id = None
        session.flush()
        session.delete(report)
        if run_id is not None:
            artifacts = session.scalars(
                select(CollectionArtifactRow).where(
                    CollectionArtifactRow.generation_run_id == run_id
                )
            ).all()
            for artifact in artifacts:
                session.delete(artifact)
            run = session.get(GenerationRunRow, run_id)
            if run is not None:
                session.delete(run)
    return True


def save_report_completion(
    database_path: Path,
    *,
    report_id: str,
    run_id: str,
    title: str,
    version: str,
    source_fingerprint: str,
    artifacts: tuple[ArtifactWrite, ...],
) -> tuple[CollectionArtifact, ...]:
    """Index the report artifact bundle and finish report and run atomically."""

    with session_scope(database_path) as session:
        report = session.get(CollectionReportRow, report_id)
        if report is None:
            raise KeyError(f"Collection report not found: {report_id}")
        rows = [
            CollectionArtifactRow(
                id=str(uuid.uuid4()),
                collection_id=report.collection_id,
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
        session.flush()
        report.title = title
        report.status = "completed"
        report.completed_at = _now(session)
        report.report_artifact_id = next(
            (row.id for row in rows if row.kind == "report_json"), None
        )
        run = session.get(GenerationRunRow, run_id)
        if run is None:
            raise KeyError(f"Generation run not found: {run_id}")
        run.status = "completed"
        run.completed_at = _now(session)
        session.flush()
        return tuple(_artifact(row) for row in rows)


def fail_report(
    database_path: Path, report_id: str, *, error_code: str, error_message: str
) -> None:
    """Fail a report and its generation run in one transaction."""

    with session_scope(database_path) as session:
        report = session.get(CollectionReportRow, report_id)
        if report is not None:
            report.status = "failed"
            report.error = f"{error_code}: {error_message}"
            report.completed_at = _now(session)
            run_id = report.run_id
        else:
            run_id = None
        if run_id is not None:
            run = session.get(GenerationRunRow, run_id)
            if run is not None:
                run.status = "failed"
                run.error_code = error_code
                run.error_message = error_message
                run.completed_at = _now(session)


def interrupt_active_reports(database_path: Path) -> int:
    """Fail queued/running reports after a process restart."""

    with session_scope(database_path) as session:
        result = session.execute(
            update(CollectionReportRow)
            .where(CollectionReportRow.status.in_(("queued", "running")))
            .values(
                status="failed",
                error="interrupted: Generation interrupted by service restart",
                completed_at=_now(session),
            )
        )
        if not isinstance(result, CursorResult):
            return 0
        return int(result.rowcount or 0)


def _report(row: CollectionReportRow) -> CollectionReportRecord:
    return CollectionReportRecord(
        id=row.id,
        collection_id=row.collection_id,
        kind=ReportKind(row.kind),
        status=cast(Literal["queued", "running", "completed", "failed"], row.status),
        title=row.title,
        user_prompt=row.user_prompt,
        source_snapshot_json=row.source_snapshot_json,
        source_fingerprint=row.source_fingerprint,
        run_id=row.run_id,
        report_artifact_id=row.report_artifact_id,
        error=row.error,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


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
