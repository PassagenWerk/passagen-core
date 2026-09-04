"""Public application-service boundary for browsing and organizing papers."""

from __future__ import annotations

import json
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import ValidationError
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from passagen.domain import PaperStatus
from passagen.stages.summarization.schema import StructuredSummary
from passagen.storage.database import SCHEMA_VERSION, current_version
from passagen.storage.engine import session_scope
from passagen.storage.models import (
    ArtifactRow,
    CollectionPaperRow,
    CollectionRow,
    PaperRow,
    PaperTagRow,
    TagRow,
)


class CatalogError(RuntimeError):
    """Base class for stable catalog failures exposed to adapters."""


class CatalogNotFoundError(CatalogError):
    pass


class CatalogConflictError(CatalogError):
    pass


class CatalogValidationError(CatalogError):
    pass


class IncompatibleSchemaError(CatalogError):
    pass


class CatalogBusyError(CatalogError):
    pass


class InvalidArtifactError(CatalogError):
    pass


class PaperSort(StrEnum):
    TITLE = "title"
    VENUE = "venue"
    YEAR = "year"
    IMPORTED_AT = "imported_at"
    UPDATED_AT = "updated_at"
    COLLECTION_ORDER = "collection_order"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class PaperFilters:
    query: str | None = None
    status: PaperStatus | None = None
    tag_id: str | None = None
    venue: str | None = None
    year: int | None = None
    collection_id: str | None = None
    unfiled: bool = False


@dataclass(frozen=True, slots=True)
class PaperView:
    id: str
    title: str | None
    authors: tuple[str, ...]
    year: int | None
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    source_url: str | None
    original_filename: str
    status: PaperStatus
    imported_at: str
    updated_at: str
    metadata_sources: dict[str, str]
    artifact_kinds: tuple[str, ...]
    tag_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperPage:
    items: tuple[PaperView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class Tag:
    id: str
    name: str
    color: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class CollectionMembership:
    paper_id: str
    position: int
    note: str | None
    added_at: str


@dataclass(frozen=True, slots=True)
class Collection:
    id: str
    name: str
    description: str | None
    created_at: str
    updated_at: str
    papers: tuple[CollectionMembership, ...] = ()


class CatalogService:
    """Facade used by the CLI, Web API, and future local integrations."""

    def __init__(self, database_path: Path, data_dir: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.data_dir = data_dir.expanduser().resolve()
        version = current_version(self.database_path)
        if version != SCHEMA_VERSION:
            found = "uninitialized" if version is None else str(version)
            raise IncompatibleSchemaError(
                f"Database schema {found} is incompatible; expected {SCHEMA_VERSION}. "
                "Run `passagen db init`."
            )

    def list_papers(
        self,
        filters: PaperFilters | None = None,
        *,
        sort: PaperSort = PaperSort.IMPORTED_AT,
        direction: SortDirection = SortDirection.DESC,
        limit: int = 50,
        offset: int = 0,
    ) -> PaperPage:
        if filters is None:
            filters = PaperFilters()
        if filters.collection_id is not None and filters.unfiled:
            raise CatalogValidationError("collection and unfiled filters are mutually exclusive")
        if not 1 <= limit <= 200 or offset < 0:
            raise CatalogValidationError("limit must be 1..200 and offset must be non-negative")
        statement = _filtered_papers(filters)
        count_statement = select(func.count()).select_from(statement.subquery())
        if sort is PaperSort.COLLECTION_ORDER and filters.collection_id is None:
            raise CatalogValidationError("collection_order requires a collection filter")
        sort_column = {
            PaperSort.TITLE: PaperRow.title,
            PaperSort.VENUE: PaperRow.venue,
            PaperSort.YEAR: PaperRow.year,
            PaperSort.IMPORTED_AT: PaperRow.created_at,
            PaperSort.UPDATED_AT: PaperRow.updated_at,
            PaperSort.COLLECTION_ORDER: CollectionPaperRow.position,
        }[sort]
        ordering = sort_column.asc() if direction is SortDirection.ASC else sort_column.desc()
        statement = statement.order_by(ordering, PaperRow.id.asc()).limit(limit).offset(offset)
        try:
            with session_scope(self.database_path) as session:
                total = int(session.scalar(count_statement) or 0)
                rows = list(session.scalars(statement).all())
                views = _paper_views(session, rows)
        except OperationalError as exc:
            raise _operational_error(exc) from exc
        return PaperPage(tuple(views), total, limit, offset)

    def get_paper(self, paper_id: str) -> PaperView:
        try:
            with session_scope(self.database_path) as session:
                row = session.get(PaperRow, paper_id)
                if row is None:
                    raise CatalogNotFoundError(f"Paper not found: {paper_id}")
                return _paper_views(session, [row])[0]
        except OperationalError as exc:
            raise _operational_error(exc) from exc

    def update_user_metadata(
        self,
        paper_id: str,
        *,
        title: str | None = None,
        venue: str | None = None,
        year: int | None = None,
        expected_updated_at: str | None = None,
    ) -> PaperView:
        if title is not None and not title.strip():
            raise CatalogValidationError("title must not be blank")
        if venue is not None and not venue.strip():
            raise CatalogValidationError("venue must not be blank")
        if year is not None and not 0 < year <= 9999:
            raise CatalogValidationError("year must be between 1 and 9999")
        values = {
            key: value.strip() if isinstance(value, str) else value
            for key, value in {
                "title": title,
                "venue": venue,
                "year": year,
            }.items()
            if value is not None
        }
        if not values:
            raise CatalogValidationError("at least one metadata field is required")
        try:
            with session_scope(self.database_path) as session:
                row = session.get(PaperRow, paper_id)
                if row is None:
                    raise CatalogNotFoundError(f"Paper not found: {paper_id}")
                if expected_updated_at is not None and row.updated_at != expected_updated_at:
                    raise CatalogConflictError("Paper metadata was modified by another operation")
                sources = _json_dict(row.metadata_sources_json)
                sources.update(dict.fromkeys(values, "user"))
                for key, value in values.items():
                    setattr(row, key, value)
                row.metadata_sources_json = json.dumps(sources, ensure_ascii=False, sort_keys=True)
                row.updated_at = str(
                    session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
                )
                session.flush()
                return _paper_views(session, [row])[0]
        except IntegrityError as exc:
            raise CatalogConflictError("Metadata conflicts with another paper") from exc
        except OperationalError as exc:
            raise _operational_error(exc) from exc

    def list_tags(self) -> tuple[Tag, ...]:
        with session_scope(self.database_path) as session:
            rows = session.scalars(select(TagRow).order_by(TagRow.normalized_name, TagRow.id)).all()
            return tuple(_tag(row) for row in rows)

    def get_tag(self, tag_id: str) -> Tag:
        with session_scope(self.database_path) as session:
            return _tag(_required(session, TagRow, tag_id, "Tag"))

    def create_tag(self, name: str, color: str | None = None) -> Tag:
        clean_name, normalized = _tag_names(name)
        try:
            with session_scope(self.database_path) as session:
                row = TagRow(
                    id=str(uuid.uuid4()), name=clean_name, normalized_name=normalized, color=color
                )
                session.add(row)
                session.flush()
                return _tag(row)
        except IntegrityError as exc:
            raise CatalogConflictError(f"Tag already exists: {clean_name}") from exc

    def update_tag(self, tag_id: str, *, name: str | None = None, color: str | None = None) -> Tag:
        try:
            with session_scope(self.database_path) as session:
                row = _required(session, TagRow, tag_id, "Tag")
                if name is not None:
                    row.name, row.normalized_name = _tag_names(name)
                row.color = color
                session.flush()
                return _tag(row)
        except IntegrityError as exc:
            raise CatalogConflictError("A tag with that name already exists") from exc

    def delete_tag(self, tag_id: str) -> None:
        with session_scope(self.database_path) as session:
            session.delete(_required(session, TagRow, tag_id, "Tag"))

    def set_paper_tags(self, paper_id: str, tag_ids: list[str]) -> tuple[Tag, ...]:
        if len(tag_ids) != len(set(tag_ids)):
            raise CatalogValidationError("tag IDs must not contain duplicates")
        with session_scope(self.database_path) as session:
            _required(session, PaperRow, paper_id, "Paper")
            tags = list(session.scalars(select(TagRow).where(TagRow.id.in_(tag_ids))).all())
            if len(tags) != len(tag_ids):
                raise CatalogNotFoundError("One or more tags do not exist")
            current = list(
                session.scalars(select(PaperTagRow).where(PaperTagRow.paper_id == paper_id)).all()
            )
            for assignment in current:
                session.delete(assignment)
            session.flush()
            session.add_all(PaperTagRow(paper_id=paper_id, tag_id=tag_id) for tag_id in tag_ids)
            return tuple(_tag(row) for row in sorted(tags, key=lambda item: item.normalized_name))

    def add_paper_tag(self, paper_id: str, tag_id: str) -> None:
        with session_scope(self.database_path) as session:
            _required(session, PaperRow, paper_id, "Paper")
            _required(session, TagRow, tag_id, "Tag")
            if session.get(PaperTagRow, (paper_id, tag_id)) is None:
                session.add(PaperTagRow(paper_id=paper_id, tag_id=tag_id))

    def remove_paper_tag(self, paper_id: str, tag_id: str) -> None:
        with session_scope(self.database_path) as session:
            _required(session, PaperRow, paper_id, "Paper")
            _required(session, TagRow, tag_id, "Tag")
            assignment = session.get(PaperTagRow, (paper_id, tag_id))
            if assignment is None:
                raise CatalogNotFoundError("Paper does not have the tag")
            session.delete(assignment)

    def list_collections(self) -> tuple[Collection, ...]:
        with session_scope(self.database_path) as session:
            rows = session.scalars(
                select(CollectionRow).order_by(CollectionRow.name, CollectionRow.id)
            )
            return tuple(_collection(row) for row in rows)

    def create_collection(self, name: str, description: str | None = None) -> Collection:
        clean_name = _required_name(name, "collection name")
        with session_scope(self.database_path) as session:
            row = CollectionRow(id=str(uuid.uuid4()), name=clean_name, description=description)
            session.add(row)
            session.flush()
            return _collection(row)

    def get_collection(self, collection_id: str) -> Collection:
        with session_scope(self.database_path) as session:
            row = _required(session, CollectionRow, collection_id, "Collection")
            memberships = session.scalars(
                select(CollectionPaperRow)
                .where(CollectionPaperRow.collection_id == collection_id)
                .order_by(CollectionPaperRow.position)
            ).all()
            return _collection(row, memberships)

    def update_collection(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Collection:
        with session_scope(self.database_path) as session:
            row = _required(session, CollectionRow, collection_id, "Collection")
            if name is not None:
                row.name = _required_name(name, "collection name")
            row.description = description
            row.updated_at = str(session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))))
            session.flush()
            return _collection(row)

    def delete_collection(self, collection_id: str) -> None:
        with session_scope(self.database_path) as session:
            session.delete(_required(session, CollectionRow, collection_id, "Collection"))

    def add_collection_papers(self, collection_id: str, paper_ids: list[str]) -> Collection:
        if len(paper_ids) != len(set(paper_ids)):
            raise CatalogValidationError("paper IDs must not contain duplicates")
        try:
            with session_scope(self.database_path) as session:
                collection = _required(session, CollectionRow, collection_id, "Collection")
                found = set(session.scalars(select(PaperRow.id).where(PaperRow.id.in_(paper_ids))))
                if found != set(paper_ids):
                    raise CatalogNotFoundError("One or more papers do not exist")
                existing = set(
                    session.scalars(
                        select(CollectionPaperRow.paper_id).where(
                            CollectionPaperRow.collection_id == collection_id
                        )
                    )
                )
                last_position = session.scalar(
                    select(func.max(CollectionPaperRow.position)).where(
                        CollectionPaperRow.collection_id == collection_id
                    )
                )
                position = (int(last_position) if last_position is not None else -1) + 1
                for paper_id in paper_ids:
                    if paper_id not in existing:
                        session.add(
                            CollectionPaperRow(
                                collection_id=collection_id,
                                paper_id=paper_id,
                                position=position,
                            )
                        )
                        position += 1
                collection.updated_at = str(
                    session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
                )
                session.flush()
                memberships = session.scalars(
                    select(CollectionPaperRow)
                    .where(CollectionPaperRow.collection_id == collection_id)
                    .order_by(CollectionPaperRow.position)
                ).all()
                return _collection(collection, memberships)
        except IntegrityError as exc:
            raise CatalogConflictError("Paper is already in the collection") from exc

    def reorder_collection(self, collection_id: str, paper_ids: list[str]) -> Collection:
        if len(paper_ids) != len(set(paper_ids)):
            raise CatalogValidationError("paper IDs must not contain duplicates")
        with session_scope(self.database_path) as session:
            collection = _required(session, CollectionRow, collection_id, "Collection")
            memberships = list(
                session.scalars(
                    select(CollectionPaperRow).where(
                        CollectionPaperRow.collection_id == collection_id
                    )
                ).all()
            )
            by_paper = {membership.paper_id: membership for membership in memberships}
            if set(paper_ids) != set(by_paper):
                raise CatalogValidationError(
                    "paper IDs must contain every collection member exactly once"
                )
            session.execute(
                delete(CollectionPaperRow).where(CollectionPaperRow.collection_id == collection_id)
            )
            session.flush()
            for position, paper_id in enumerate(paper_ids):
                previous = by_paper[paper_id]
                session.add(
                    CollectionPaperRow(
                        collection_id=collection_id,
                        paper_id=paper_id,
                        position=position,
                        note=previous.note,
                        added_at=previous.added_at,
                    )
                )
            collection.updated_at = str(
                session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
            )
            session.flush()
            session.flush()
            reordered = list(
                session.scalars(
                    select(CollectionPaperRow)
                    .where(CollectionPaperRow.collection_id == collection_id)
                    .order_by(CollectionPaperRow.position)
                )
            )
            return _collection(collection, reordered)

    def remove_collection_paper(self, collection_id: str, paper_id: str) -> Collection:
        with session_scope(self.database_path) as session:
            collection = _required(session, CollectionRow, collection_id, "Collection")
            membership = session.get(CollectionPaperRow, (collection_id, paper_id))
            if membership is None:
                raise CatalogNotFoundError("Paper is not in the collection")
            session.delete(membership)
            session.flush()
            remaining = list(
                session.scalars(
                    select(CollectionPaperRow)
                    .where(CollectionPaperRow.collection_id == collection_id)
                    .order_by(CollectionPaperRow.position)
                ).all()
            )
            session.execute(
                delete(CollectionPaperRow).where(CollectionPaperRow.collection_id == collection_id)
            )
            session.flush()
            for position, item in enumerate(remaining):
                session.add(
                    CollectionPaperRow(
                        collection_id=collection_id,
                        paper_id=item.paper_id,
                        position=position,
                        note=item.note,
                        added_at=item.added_at,
                    )
                )
            collection.updated_at = str(
                session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
            )
            session.flush()
            compacted = list(
                session.scalars(
                    select(CollectionPaperRow)
                    .where(CollectionPaperRow.collection_id == collection_id)
                    .order_by(CollectionPaperRow.position)
                )
            )
            return _collection(collection, compacted)

    def resolve_artifact(self, paper_id: str, kind: str) -> Path:
        with session_scope(self.database_path) as session:
            _required(session, PaperRow, paper_id, "Paper")
            row = session.scalar(
                select(ArtifactRow)
                .where(ArtifactRow.paper_id == paper_id, ArtifactRow.kind == kind)
                .order_by(ArtifactRow.created_at.desc(), ArtifactRow.id.desc())
                .limit(1)
            )
            if row is None:
                raise CatalogNotFoundError(f"Artifact not found: {kind}")
            relative = Path(row.path)
            if relative.is_absolute():
                raise InvalidArtifactError("Artifact path must be relative")
            resolved = (self.data_dir / relative).resolve()
            if not resolved.is_relative_to(self.data_dir):
                raise InvalidArtifactError("Artifact path escapes the data directory")
            if not resolved.is_file():
                raise InvalidArtifactError("Artifact file is missing")
            if row.size_bytes is not None and resolved.stat().st_size != row.size_bytes:
                raise InvalidArtifactError("Artifact size does not match its catalog record")
            return resolved


def _filtered_papers(filters: PaperFilters) -> Select[tuple[PaperRow]]:
    statement = select(PaperRow)
    if filters.query:
        statement = statement.where(PaperRow.title.ilike(f"%{filters.query.strip()}%"))
    if filters.status is not None:
        statement = statement.where(PaperRow.status == filters.status.value)
    if filters.venue:
        statement = statement.where(PaperRow.venue == filters.venue)
    if filters.year is not None:
        statement = statement.where(PaperRow.year == filters.year)
    if filters.tag_id:
        statement = statement.join(PaperTagRow).where(PaperTagRow.tag_id == filters.tag_id)
    if filters.collection_id:
        statement = statement.join(CollectionPaperRow).where(
            CollectionPaperRow.collection_id == filters.collection_id
        )
    elif filters.unfiled:
        statement = statement.where(
            ~select(CollectionPaperRow.paper_id)
            .where(CollectionPaperRow.paper_id == PaperRow.id)
            .exists()
        )
    return statement


def _paper_views(session: Session, rows: list[PaperRow]) -> list[PaperView]:
    ids = [row.id for row in rows]
    artifact_map: dict[str, set[str]] = {paper_id: set() for paper_id in ids}
    tag_map: dict[str, list[str]] = {paper_id: [] for paper_id in ids}
    if ids:
        for paper_id, kind in session.execute(
            select(ArtifactRow.paper_id, ArtifactRow.kind).where(ArtifactRow.paper_id.in_(ids))
        ):
            artifact_map[str(paper_id)].add(str(kind))
        for paper_id, tag_id in session.execute(
            select(PaperTagRow.paper_id, PaperTagRow.tag_id)
            .join(TagRow, TagRow.id == PaperTagRow.tag_id)
            .where(PaperTagRow.paper_id.in_(ids))
            .order_by(TagRow.normalized_name, TagRow.id)
        ):
            tag_map[str(paper_id)].append(str(tag_id))
    return [
        PaperView(
            id=row.id,
            title=row.title,
            authors=tuple(str(value) for value in _json_list(row.authors_json)),
            year=row.year,
            venue=row.venue,
            doi=row.doi,
            arxiv_id=row.arxiv_id,
            source_url=row.source_url,
            original_filename=row.original_filename,
            status=PaperStatus(row.status),
            imported_at=row.created_at,
            updated_at=row.updated_at,
            metadata_sources={
                str(key): str(value) for key, value in _json_dict(row.metadata_sources_json).items()
            },
            artifact_kinds=tuple(sorted(artifact_map[row.id])),
            tag_ids=tuple(tag_map[row.id]),
        )
        for row in rows
    ]


def _tag(row: TagRow) -> Tag:
    return Tag(row.id, row.name, row.color, row.created_at)


def _collection(
    row: CollectionRow, memberships: Sequence[CollectionPaperRow] | None = None
) -> Collection:
    papers = tuple(
        CollectionMembership(item.paper_id, item.position, item.note, item.added_at)
        for item in (memberships or [])
    )
    return Collection(row.id, row.name, row.description, row.created_at, row.updated_at, papers)


def _required[ModelRow: (TagRow, CollectionRow, PaperRow)](
    session: Session,
    model: type[ModelRow],
    object_id: str,
    label: str,
) -> ModelRow:
    row = session.get(model, object_id)
    if row is None:
        raise CatalogNotFoundError(f"{label} not found: {object_id}")
    return row


def _required_name(value: str, label: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise CatalogValidationError(f"{label} must not be blank")
    return cleaned


def _tag_names(name: str) -> tuple[str, str]:
    cleaned = _required_name(name, "tag name")
    return cleaned, unicodedata.normalize("NFKC", cleaned).casefold()


def _json_list(value: str | None) -> list[object]:
    if not value:
        return []
    parsed = json.loads(value)
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    return {str(key): str(item) for key, item in parsed.items()} if isinstance(parsed, dict) else {}


def _operational_error(exc: OperationalError) -> CatalogError:
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return CatalogBusyError("The paper catalog is busy; retry the operation")
    return CatalogError("The paper catalog operation failed")


def validate_summary_json(content: str | bytes) -> dict[str, object]:
    """Validate and decode a generated summary without exposing pipeline models."""
    try:
        summary = StructuredSummary.model_validate_json(content)
    except ValidationError as exc:
        raise InvalidArtifactError("Summary artifact does not match the supported schema") from exc
    return cast(dict[str, object], summary.model_dump(mode="json"))


__all__ = [
    "CatalogBusyError",
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogService",
    "CatalogValidationError",
    "Collection",
    "CollectionMembership",
    "IncompatibleSchemaError",
    "InvalidArtifactError",
    "PaperFilters",
    "PaperPage",
    "PaperSort",
    "PaperStatus",
    "PaperView",
    "SortDirection",
    "Tag",
    "validate_summary_json",
]
