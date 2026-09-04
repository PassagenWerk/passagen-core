"""Persistence for batch update runs (``update_runs`` table)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from passagen.processing.models import (
    ACTIVE_RUN_STATUSES,
    ProcessingRun,
    RunMode,
    RunPaperFailure,
    RunResultSummary,
    RunStatus,
)
from passagen.storage.engine import session_scope
from passagen.storage.models import UpdateRunRow
from passagen.storage.repository import DatabaseNotInitializedError

_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def _require_database(database_path: Path) -> None:
    if not database_path.exists():
        raise DatabaseNotInitializedError(f"Database is not initialized: {database_path}")


def create_run(
    database_path: Path,
    *,
    paper_ids: list[str],
    mode: RunMode,
    from_stage: str | None,
) -> ProcessingRun:
    _require_database(database_path)
    run_id = str(uuid.uuid4())
    with session_scope(database_path) as session:
        session.add(
            UpdateRunRow(
                id=run_id,
                paper_ids_json=json.dumps(paper_ids),
                mode=mode,
                from_stage=from_stage,
                status=RunStatus.QUEUED.value,
            )
        )
        row = session.get(UpdateRunRow, run_id)
        if row is None:
            raise RuntimeError(f"Failed to create run {run_id}")
        session.flush()
        session.refresh(row)
        return _run_record(row)


def get_run(database_path: Path, run_id: str) -> ProcessingRun | None:
    _require_database(database_path)
    with session_scope(database_path) as session:
        row = session.get(UpdateRunRow, run_id)
        return _run_record(row) if row is not None else None


def list_runs(
    database_path: Path,
    *,
    status: RunStatus | None = None,
    paper_id: str | None = None,
    limit: int = 50,
) -> list[ProcessingRun]:
    _require_database(database_path)
    with session_scope(database_path) as session:
        statement = select(UpdateRunRow)
        if status is not None:
            statement = statement.where(UpdateRunRow.status == status.value)
        if paper_id is not None:
            statement = statement.where(UpdateRunRow.paper_ids_json.like(f'%"{paper_id}"%'))
        rows = session.scalars(
            statement.order_by(UpdateRunRow.created_at.desc(), UpdateRunRow.id.desc()).limit(limit)
        ).all()
        runs = [_run_record(row) for row in rows]
    if paper_id is not None:
        # The LIKE prefilter is not exact; filter precisely on decoded ids.
        runs = [run for run in runs if paper_id in run.paper_ids]
    return runs


def find_active_run_for_papers(database_path: Path, paper_ids: list[str]) -> ProcessingRun | None:
    if not paper_ids:
        return None
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(UpdateRunRow).where(
                UpdateRunRow.status.in_([status.value for status in ACTIVE_RUN_STATUSES])
            )
        ).all()
        requested = set(paper_ids)
        for row in rows:
            if requested & set(_decode_paper_ids(row.paper_ids_json)):
                return _run_record(row)
    return None


def claim_run(database_path: Path, run_id: str) -> bool:
    """Atomically move a queued run to running; returns False if already claimed."""
    with session_scope(database_path) as session:
        result = session.execute(
            update(UpdateRunRow)
            .where(UpdateRunRow.id == run_id, UpdateRunRow.status == RunStatus.QUEUED.value)
            .values(status=RunStatus.RUNNING.value)
        )
        return isinstance(result, CursorResult) and result.rowcount == 1


def update_run_progress(
    database_path: Path,
    run_id: str,
    *,
    current_paper_id: str | None,
    current_stage: str | None,
) -> None:
    with session_scope(database_path) as session:
        session.execute(
            update(UpdateRunRow)
            .where(UpdateRunRow.id == run_id)
            .values(current_paper_id=current_paper_id, current_stage=current_stage)
        )


def finish_run(
    database_path: Path,
    run_id: str,
    *,
    status: RunStatus,
    error: str | None = None,
    result: RunResultSummary | None = None,
) -> None:
    with session_scope(database_path) as session:
        session.execute(
            update(UpdateRunRow)
            .where(UpdateRunRow.id == run_id)
            .values(
                status=status.value,
                error=error,
                result_json=result.model_dump_json() if result is not None else None,
                finished_at=func.current_timestamp(),
            )
        )


def interrupt_active_runs(database_path: Path) -> list[ProcessingRun]:
    """Mark every queued/running run as interrupted; used at process startup."""
    _require_database(database_path)
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(UpdateRunRow).where(
                UpdateRunRow.status.in_([status.value for status in ACTIVE_RUN_STATUSES])
            )
        ).all()
        interrupted = [_run_record(row) for row in rows]
        session.execute(
            update(UpdateRunRow)
            .where(UpdateRunRow.status.in_([status.value for status in ACTIVE_RUN_STATUSES]))
            .values(
                status=RunStatus.INTERRUPTED.value,
                error="Process stopped while the run was still active",
                finished_at=func.current_timestamp(),
            )
        )
    return interrupted


def next_queued_run(database_path: Path) -> ProcessingRun | None:
    _require_database(database_path)
    with session_scope(database_path) as session:
        row = session.scalars(
            select(UpdateRunRow)
            .where(UpdateRunRow.status == RunStatus.QUEUED.value)
            .order_by(UpdateRunRow.created_at, UpdateRunRow.id)
            .limit(1)
        ).first()
        return _run_record(row) if row is not None else None


def _run_record(row: UpdateRunRow) -> ProcessingRun:
    result = RunResultSummary.model_validate_json(row.result_json) if row.result_json else None
    return ProcessingRun(
        id=row.id,
        paper_ids=_decode_paper_ids(row.paper_ids_json),
        mode=row.mode,  # type: ignore[arg-type]
        from_stage=row.from_stage,
        status=RunStatus(row.status),
        current_paper_id=row.current_paper_id,
        current_stage=row.current_stage,
        created_at=_parse_timestamp(row.created_at),
        finished_at=_parse_timestamp(row.finished_at) if row.finished_at else None,
        error=row.error,
        result=result,
    )


def _decode_paper_ids(raw: str) -> list[str]:
    parsed = json.loads(raw)
    return [str(paper_id) for paper_id in parsed] if isinstance(parsed, list) else []


def _parse_timestamp(value: str) -> datetime:
    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, timestamp_format)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


__all__ = [
    "RunPaperFailure",
    "RunResultSummary",
    "claim_run",
    "create_run",
    "find_active_run_for_papers",
    "finish_run",
    "get_run",
    "interrupt_active_runs",
    "list_runs",
    "next_queued_run",
    "update_run_progress",
]
