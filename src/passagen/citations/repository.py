from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from passagen.storage.engine import session_scope
from passagen.storage.models import PaperCitationRow


@dataclass(frozen=True, slots=True)
class CitationRecord:
    paper_id: str
    format: str
    content: str
    citation_key: str
    source: str
    authoritative: bool
    source_identifier: str | None
    metadata_fingerprint: str
    generator_version: str
    remote_status: str
    remote_checked_at: str | None
    next_remote_attempt_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CitationWrite:
    paper_id: str
    content: str
    citation_key: str
    source: str
    authoritative: bool
    source_identifier: str | None
    metadata_fingerprint: str
    generator_version: str
    remote_status: str
    remote_checked_at: str | None = None
    next_remote_attempt_at: str | None = None
    last_error: str | None = None
    format: str = "bibtex"


class CitationKeyConflictError(RuntimeError):
    pass


def get_citation(
    database_path: Path, paper_id: str, format: str = "bibtex"
) -> CitationRecord | None:
    with session_scope(database_path) as session:
        row = session.get(PaperCitationRow, (paper_id, format))
        return _record(row) if row is not None else None


def upsert_citation(database_path: Path, citation: CitationWrite) -> CitationRecord:
    values = {
        "paper_id": citation.paper_id,
        "format": citation.format,
        "content": citation.content,
        "citation_key": citation.citation_key,
        "source": citation.source,
        "authoritative": int(citation.authoritative),
        "source_identifier": citation.source_identifier,
        "metadata_fingerprint": citation.metadata_fingerprint,
        "generator_version": citation.generator_version,
        "remote_status": citation.remote_status,
        "remote_checked_at": citation.remote_checked_at,
        "next_remote_attempt_at": citation.next_remote_attempt_at,
        "last_error": citation.last_error,
    }
    try:
        with session_scope(database_path) as session:
            statement = insert(PaperCitationRow).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PaperCitationRow.paper_id, PaperCitationRow.format],
                    set_={
                        **values,
                        "updated_at": func.strftime("%Y-%m-%dT%H:%M:%fZ", "now"),
                    },
                )
            )
            row = session.scalar(
                select(PaperCitationRow).where(
                    PaperCitationRow.paper_id == citation.paper_id,
                    PaperCitationRow.format == citation.format,
                )
            )
            if row is None:
                raise RuntimeError(f"Failed to persist citation for paper {citation.paper_id}")
            return _record(row)
    except IntegrityError as exc:
        if "paper_citations.format, paper_citations.citation_key" in str(exc.orig):
            raise CitationKeyConflictError(citation.citation_key) from exc
        raise


def list_local_citation_keys(database_path: Path, *, exclude_paper_id: str) -> set[str]:
    with session_scope(database_path) as session:
        return set(
            session.scalars(
                select(PaperCitationRow.citation_key).where(
                    PaperCitationRow.paper_id != exclude_paper_id,
                    PaperCitationRow.source.in_(("arxiv", "local_metadata")),
                )
            )
        )


def _record(row: PaperCitationRow) -> CitationRecord:
    return CitationRecord(
        paper_id=row.paper_id,
        format=row.format,
        content=row.content,
        citation_key=row.citation_key,
        source=row.source,
        authoritative=bool(row.authoritative),
        source_identifier=row.source_identifier,
        metadata_fingerprint=row.metadata_fingerprint,
        generator_version=row.generator_version,
        remote_status=row.remote_status,
        remote_checked_at=row.remote_checked_at,
        next_remote_attempt_at=row.next_remote_attempt_at,
        last_error=row.last_error,
        created_at=_timestamp(row.created_at),
        updated_at=_timestamp(row.updated_at),
    )


def _timestamp(value: str) -> str:
    return value.replace(" ", "T") + ("Z" if "Z" not in value and "+" not in value else "")


__all__ = [
    "CitationKeyConflictError",
    "CitationRecord",
    "CitationWrite",
    "get_citation",
    "list_local_citation_keys",
    "upsert_citation",
]
