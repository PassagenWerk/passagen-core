import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from passagen.domain import BibliographicMetadata, Paper, PaperStatus
from passagen.storage.engine import session_scope
from passagen.storage.models import ArtifactRow, LlmCallRow, PaperRow, ProcessingRunRow


class DatabaseNotInitializedError(RuntimeError):
    pass


class MetadataConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaperRecord:
    id: str
    original_filename: str
    pdf_sha256: str
    status: PaperStatus
    title: str | None
    abstract: str | None
    authors: tuple[str, ...]
    year: int | None
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    source_url: str | None
    metadata_sources: dict[str, str]
    managed_pdf_path: Path | None
    file_size_bytes: int | None
    imported_at: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    paper_id: str
    kind: str
    path: Path
    version: str | None
    sha256: str | None
    size_bytes: int | None


def register_pdf(
    database_path: Path,
    paper: Paper,
    managed_path: Path,
) -> tuple[PaperRecord, bool]:
    with session_scope(database_path) as session:
        inserted_id = session.scalar(
            insert(PaperRow)
            .values(
                id=paper.id,
                original_filename=paper.original_filename,
                pdf_sha256=paper.pdf_sha256,
                status=paper.status.value,
            )
            .on_conflict_do_nothing(index_elements=[PaperRow.pdf_sha256])
            .returning(PaperRow.id)
        )
        created = inserted_id is not None
        if created:
            session.add(
                ArtifactRow(
                    id=str(uuid.uuid4()),
                    paper_id=paper.id,
                    kind="original_pdf",
                    path=managed_path.as_posix(),
                    sha256=paper.pdf_sha256,
                    size_bytes=paper.file_size_bytes,
                )
            )
        row = _paper_by_sha256(session, paper.pdf_sha256)
        if row is None:
            raise RuntimeError(f"Failed to register PDF {paper.pdf_sha256}")
        return _paper_record(row), created


def find_paper_by_sha256(database_path: Path, sha256: str) -> PaperRecord | None:
    _require_database(database_path)
    with session_scope(database_path) as session:
        row = _paper_by_sha256(session, sha256)
        return _paper_record(row) if row is not None else None


def list_papers(
    database_path: Path,
    status: PaperStatus | None = None,
) -> list[PaperRecord]:
    _require_database(database_path)
    with session_scope(database_path) as session:
        statement = select(PaperRow).options(selectinload(PaperRow.artifacts))
        if status is not None:
            statement = statement.where(PaperRow.status == status.value)
        rows = session.scalars(statement.order_by(PaperRow.created_at, PaperRow.id)).all()
        return [_paper_record(row) for row in rows]


def get_paper(database_path: Path, paper_id: str) -> PaperRecord | None:
    _require_database(database_path)
    with session_scope(database_path) as session:
        row = _paper_by_id(session, paper_id)
        return _paper_record(row) if row is not None else None


def update_paper_metadata(
    database_path: Path,
    paper_id: str,
    metadata: BibliographicMetadata,
    status: PaperStatus,
) -> PaperRecord:
    _require_database(database_path)
    try:
        with session_scope(database_path) as session:
            existing = _paper_by_id(session, paper_id)
            if existing is None:
                raise KeyError(paper_id)
            values = _metadata_values_preserving_user_sources(existing, metadata)
            result = session.execute(
                update(PaperRow)
                .where(PaperRow.id == paper_id)
                .values(
                    **values,
                    status=status.value,
                    updated_at=func.strftime("%Y-%m-%d %H:%M:%f", "now"),
                )
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                raise KeyError(paper_id)
            row = _paper_by_id(session, paper_id)
            if row is None:
                raise RuntimeError(f"Failed to reload paper {paper_id}")
            return _paper_record(row)
    except IntegrityError as exc:
        raise MetadataConflictError(
            f"DOI or arXiv ID is already assigned to another paper: {exc.orig}"
        ) from exc


def update_paper_abstract(
    database_path: Path,
    paper_id: str,
    abstract: str,
    *,
    source: str,
    overwrite: bool = False,
) -> tuple[PaperRecord, bool]:
    _require_database(database_path)
    clean_abstract = " ".join(abstract.split())
    if not clean_abstract:
        raise ValueError("abstract must not be blank")
    with session_scope(database_path) as session:
        row = _paper_by_id(session, paper_id)
        if row is None:
            raise KeyError(paper_id)
        sources = {
            str(key): str(value) for key, value in _json_dict(row.metadata_sources_json).items()
        }
        if sources.get("abstract") == "user" or (row.abstract and not overwrite):
            return _paper_record(row), False
        row.abstract = clean_abstract
        sources["abstract"] = source
        row.metadata_sources_json = json.dumps(sources, ensure_ascii=False, sort_keys=True)
        row.updated_at = str(session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))))
        session.flush()
        return _paper_record(row), True


def get_artifact(
    database_path: Path,
    paper_id: str,
    kind: str,
) -> ArtifactRecord | None:
    _require_database(database_path)
    with session_scope(database_path) as session:
        row = _latest_artifact(session, paper_id, kind)
        return _artifact_record(row) if row is not None else None


def list_artifacts(database_path: Path) -> list[ArtifactRecord]:
    _require_database(database_path)
    with session_scope(database_path) as session:
        rows = session.scalars(
            select(ArtifactRow).order_by(ArtifactRow.created_at, ArtifactRow.id)
        ).all()
        return [_artifact_record(row) for row in rows]


def save_parsed_artifact(
    database_path: Path,
    paper_id: str,
    path: Path,
    *,
    version: str,
    sha256: str,
    size_bytes: int,
    status: PaperStatus,
    abstract: str | None = None,
    abstract_source: str | None = None,
) -> tuple[PaperRecord, ArtifactRecord]:
    _require_database(database_path)
    with session_scope(database_path) as session:
        artifact = _upsert_artifact(
            session, paper_id, "extracted_json", path, version, sha256, size_bytes
        )
        if abstract and abstract_source:
            paper = session.get(PaperRow, paper_id)
            if paper is None:
                raise KeyError(paper_id)
            sources = {
                str(key): str(value)
                for key, value in _json_dict(paper.metadata_sources_json).items()
            }
            if sources.get("abstract") != "user":
                paper.abstract = abstract
                sources["abstract"] = abstract_source
                paper.metadata_sources_json = json.dumps(
                    sources, ensure_ascii=False, sort_keys=True
                )
        _update_paper_status(session, paper_id, status)
        paper = _paper_by_id(session, paper_id)
        if paper is None:
            raise RuntimeError(f"Failed to reload parsed artifact for {paper_id}")
        return _paper_record(paper), artifact


def save_summary_artifacts(
    database_path: Path,
    paper_id: str,
    json_path: Path,
    yaml_path: Path,
    *,
    version: str,
    json_sha256: str,
    json_size_bytes: int,
    yaml_sha256: str,
    yaml_size_bytes: int,
) -> tuple[PaperRecord, ArtifactRecord]:
    _require_database(database_path)
    with session_scope(database_path) as session:
        summary = _upsert_artifact(
            session,
            paper_id,
            "summary_json",
            json_path,
            version,
            json_sha256,
            json_size_bytes,
        )
        _upsert_artifact(
            session,
            paper_id,
            "summary_yaml",
            yaml_path,
            version,
            yaml_sha256,
            yaml_size_bytes,
        )
        _update_paper_status(session, paper_id, PaperStatus.SUMMARIZED)
        paper = _paper_by_id(session, paper_id)
        if paper is None:
            raise RuntimeError(f"Failed to reload summary artifact for {paper_id}")
        return _paper_record(paper), summary


def save_outline_artifacts(
    database_path: Path,
    paper_id: str,
    markdown_path: Path,
    source_path: Path,
    *,
    version: str,
    markdown_sha256: str,
    markdown_size_bytes: int,
    source_sha256: str,
    source_size_bytes: int,
) -> tuple[PaperRecord, ArtifactRecord]:
    _require_database(database_path)
    with session_scope(database_path) as session:
        outline = _upsert_artifact(
            session,
            paper_id,
            "outline_md",
            markdown_path,
            version,
            markdown_sha256,
            markdown_size_bytes,
        )
        _upsert_artifact(
            session,
            paper_id,
            "outline_source_json",
            source_path,
            version,
            source_sha256,
            source_size_bytes,
        )
        _update_paper_status(session, paper_id, PaperStatus.OUTLINED)
        paper = _paper_by_id(session, paper_id)
        if paper is None:
            raise RuntimeError(f"Failed to reload outline artifact for {paper_id}")
        return _paper_record(paper), outline


def start_processing_run(database_path: Path, paper_id: str, stage: str) -> str:
    _require_database(database_path)
    run_id = str(uuid.uuid4())
    with session_scope(database_path) as session:
        session.add(ProcessingRunRow(id=run_id, paper_id=paper_id, stage=stage, status="running"))
    return run_id


def finish_processing_run(
    database_path: Path,
    run_id: str,
    *,
    error_message: str | None = None,
) -> None:
    with session_scope(database_path) as session:
        session.execute(
            update(ProcessingRunRow)
            .where(ProcessingRunRow.id == run_id)
            .values(
                status="failed" if error_message else "completed",
                error_message=error_message,
                finished_at=func.current_timestamp(),
            )
        )


def update_paper_status(database_path: Path, paper_id: str, status: PaperStatus) -> None:
    _require_database(database_path)
    with session_scope(database_path) as session:
        _update_paper_status(session, paper_id, status)


def record_llm_call(
    database_path: Path,
    processing_run_id: str,
    *,
    provider: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    input_tokens: int | None,
    output_tokens: int | None,
    error_message: str | None = None,
) -> None:
    with session_scope(database_path) as session:
        session.add(
            LlmCallRow(
                id=str(uuid.uuid4()),
                processing_run_id=processing_run_id,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_message=error_message,
            )
        )


def managed_path_is_referenced(database_path: Path, managed_path: Path) -> bool:
    if not database_path.exists():
        return False
    with session_scope(database_path) as session:
        return (
            session.scalar(
                select(ArtifactRow.id).where(ArtifactRow.path == managed_path.as_posix()).limit(1)
            )
            is not None
        )


def _paper_by_id(session: Session, paper_id: str) -> PaperRow | None:
    return session.scalar(
        select(PaperRow).options(selectinload(PaperRow.artifacts)).where(PaperRow.id == paper_id)
    )


def _paper_by_sha256(session: Session, sha256: str) -> PaperRow | None:
    return session.scalar(
        select(PaperRow)
        .options(selectinload(PaperRow.artifacts))
        .where(PaperRow.pdf_sha256 == sha256)
    )


def _latest_artifact(session: Session, paper_id: str, kind: str) -> ArtifactRow | None:
    return session.scalar(
        select(ArtifactRow)
        .where(ArtifactRow.paper_id == paper_id, ArtifactRow.kind == kind)
        .order_by(ArtifactRow.created_at.desc())
        .limit(1)
    )


def _upsert_artifact(
    session: Session,
    paper_id: str,
    kind: str,
    path: Path,
    version: str,
    sha256: str,
    size_bytes: int,
) -> ArtifactRecord:
    row = _latest_artifact(session, paper_id, kind)
    if row is None:
        row = ArtifactRow(
            id=str(uuid.uuid4()),
            paper_id=paper_id,
            kind=kind,
            path=path.as_posix(),
            version=version,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        session.add(row)
    else:
        session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id == row.id)
            .values(
                path=path.as_posix(),
                version=version,
                sha256=sha256,
                size_bytes=size_bytes,
                created_at=func.current_timestamp(),
            )
        )
        session.refresh(row)
    session.flush()
    return _artifact_record(row)


def _update_paper_status(session: Session, paper_id: str, status: PaperStatus) -> None:
    result = session.execute(
        update(PaperRow)
        .where(PaperRow.id == paper_id)
        .values(status=status.value, updated_at=func.current_timestamp())
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        raise KeyError(paper_id)


def _paper_record(row: PaperRow) -> PaperRecord:
    original = max(
        (artifact for artifact in row.artifacts if artifact.kind == "original_pdf"),
        key=lambda artifact: (artifact.created_at, artifact.id),
        default=None,
    )
    authors = _json_list(row.authors_json)
    sources = _json_dict(row.metadata_sources_json)
    return PaperRecord(
        id=row.id,
        original_filename=row.original_filename,
        pdf_sha256=row.pdf_sha256,
        status=PaperStatus(row.status),
        title=row.title,
        abstract=row.abstract,
        authors=tuple(str(author) for author in authors),
        year=row.year,
        venue=row.venue,
        doi=row.doi,
        arxiv_id=row.arxiv_id,
        source_url=row.source_url,
        metadata_sources={str(key): str(value) for key, value in sources.items()},
        managed_pdf_path=Path(original.path) if original is not None else None,
        file_size_bytes=original.size_bytes if original is not None else None,
        imported_at=row.created_at,
    )


def _artifact_record(row: ArtifactRow) -> ArtifactRecord:
    return ArtifactRecord(
        id=row.id,
        paper_id=row.paper_id,
        kind=row.kind,
        path=Path(row.path),
        version=row.version,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
    )


def _require_database(database_path: Path) -> None:
    if not database_path.exists():
        raise DatabaseNotInitializedError(f"Database is not initialized: {database_path}")


def _json_list(value: object) -> list[object]:
    if not isinstance(value, str) or not value:
        return []
    parsed = json.loads(value)
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: object) -> dict[object, object]:
    if not isinstance(value, str) or not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _metadata_values_preserving_user_sources(
    row: PaperRow, metadata: BibliographicMetadata
) -> dict[str, object]:
    sources = {str(key): str(value) for key, value in _json_dict(row.metadata_sources_json).items()}
    generated: dict[str, object] = {
        "title": metadata.title,
        "abstract": metadata.abstract,
        "authors": metadata.authors,
        "year": metadata.year,
        "venue": metadata.venue,
        "doi": metadata.doi,
        "arxiv_id": metadata.arxiv_id,
        "source_url": metadata.source_url,
    }
    existing: dict[str, object] = {
        "title": row.title,
        "abstract": row.abstract,
        "authors": tuple(str(author) for author in _json_list(row.authors_json)),
        "year": row.year,
        "venue": row.venue,
        "doi": row.doi,
        "arxiv_id": row.arxiv_id,
        "source_url": row.source_url,
    }
    merged_sources = dict(metadata.sources)
    if metadata.abstract in (None, "") and row.abstract:
        generated["abstract"] = row.abstract
        if abstract_source := sources.get("abstract"):
            merged_sources["abstract"] = abstract_source
    for field, source in sources.items():
        if source == "user" and field in generated:
            generated[field] = existing[field]
            merged_sources[field] = "user"
    return {
        "title": generated["title"],
        "abstract": generated["abstract"],
        "authors_json": json.dumps(generated["authors"], ensure_ascii=False),
        "year": generated["year"],
        "venue": generated["venue"],
        "doi": generated["doi"],
        "arxiv_id": generated["arxiv_id"],
        "source_url": generated["source_url"],
        "metadata_sources_json": json.dumps(merged_sources, ensure_ascii=False, sort_keys=True),
    }
