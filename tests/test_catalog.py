import sqlite3
from pathlib import Path

import pytest

from passagen.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogService,
    CatalogValidationError,
    IncompatibleSchemaError,
    InvalidArtifactError,
    PaperFilters,
    PaperSort,
    SortDirection,
    TagMatch,
    validate_summary_json,
)
from passagen.domain import BibliographicMetadata, PaperStatus
from passagen.storage.database import connect_database, initialize_database
from passagen.storage.repository import update_paper_abstract, update_paper_metadata


def _library(tmp_path: Path, count: int = 3) -> tuple[CatalogService, Path]:
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        for index in range(count):
            connection.execute(
                """
                INSERT INTO papers
                    (id, title, year, venue, original_filename, pdf_sha256, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"paper-{index}",
                    f"Paper {index}",
                    2020 + index,
                    "Venue A" if index < 2 else "Venue B",
                    f"paper-{index}.pdf",
                    str(index) * 64,
                    PaperStatus.DISCOVERED.value,
                ),
            )
    return CatalogService(database_path, tmp_path), database_path


def test_catalog_rejects_uninitialized_or_incompatible_database(tmp_path: Path) -> None:
    with pytest.raises(IncompatibleSchemaError, match="uninitialized"):
        CatalogService(tmp_path / "missing.db", tmp_path)

    database_path = tmp_path / "future.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(IncompatibleSchemaError, match="999"):
        CatalogService(database_path, tmp_path)


def test_paper_list_filters_sorts_and_paginates(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)

    page = catalog.list_papers(
        PaperFilters(query="paper", venue="Venue A"),
        sort=PaperSort.YEAR,
        direction=SortDirection.DESC,
        limit=1,
    )

    assert page.total == 2
    assert [paper.id for paper in page.items] == ["paper-1"]
    assert page.items[0].artifact_kinds == ()
    with pytest.raises(CatalogValidationError):
        catalog.list_papers(limit=0)


def test_paper_list_filters_by_multiple_tags(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path, count=4)
    systems = catalog.create_tag("Systems")
    priority = catalog.create_tag("Priority")
    catalog.add_paper_tag("paper-0", systems.id)
    catalog.add_paper_tag("paper-1", systems.id)
    catalog.add_paper_tag("paper-1", priority.id)

    both = catalog.list_papers(PaperFilters(tag_ids=(systems.id, priority.id)))
    assert both.total == 1
    assert [paper.id for paper in both.items] == ["paper-1"]

    either = catalog.list_papers(
        PaperFilters(tag_ids=(systems.id, priority.id), tag_match=TagMatch.ANY)
    )
    assert either.total == 2
    assert [paper.id for paper in either.items] == ["paper-0", "paper-1"]

    single = catalog.list_papers(PaperFilters(tag_ids=(systems.id,)))
    assert single.total == 2

    duplicated = catalog.list_papers(PaperFilters(tag_ids=(systems.id, systems.id, priority.id)))
    assert duplicated.total == 1

    unknown = catalog.list_papers(PaperFilters(tag_ids=(systems.id, "missing-tag")))
    assert unknown.total == 0
    assert unknown.items == ()


def test_tag_usage_reports_assignment_counts(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    systems = catalog.create_tag("Systems")
    unused = catalog.create_tag("Unused")
    catalog.add_paper_tag("paper-0", systems.id)
    catalog.add_paper_tag("paper-1", systems.id)

    usage = catalog.list_tag_usage()

    assert [(tag.name, tag.paper_count) for tag in usage] == [("Systems", 2), ("Unused", 0)]
    assert usage[0].color is None
    catalog.delete_tag(unused.id)
    assert [tag.name for tag in catalog.list_tag_usage()] == ["Systems"]


def test_tags_are_normalized_and_assignments_are_replaced(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    reading = catalog.create_tag("  Deep   Reading ", "#123456")
    methods = catalog.create_tag("Methods")

    with pytest.raises(CatalogConflictError, match="already exists"):
        catalog.create_tag("deep reading")

    assigned = catalog.set_paper_tags("paper-0", [methods.id, reading.id])
    assert [tag.name for tag in assigned] == ["Deep Reading", "Methods"]
    assert catalog.get_paper("paper-0").tag_ids == (reading.id, methods.id)

    catalog.set_paper_tags("paper-0", [methods.id])
    assert catalog.get_paper("paper-0").tag_ids == (methods.id,)
    with pytest.raises(CatalogValidationError, match="duplicates"):
        catalog.set_paper_tags("paper-0", [methods.id, methods.id])


def test_single_tag_assignment_is_idempotent_and_removable(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    tag = catalog.create_tag("Reading")

    catalog.add_paper_tag("paper-0", tag.id)
    catalog.add_paper_tag("paper-0", tag.id)

    assert catalog.get_tag(tag.id) == tag
    assert catalog.get_paper("paper-0").tag_ids == (tag.id,)
    catalog.remove_paper_tag("paper-0", tag.id)
    assert catalog.get_paper("paper-0").tag_ids == ()
    with pytest.raises(CatalogNotFoundError, match="does not have"):
        catalog.remove_paper_tag("paper-0", tag.id)


def test_collection_membership_is_idempotent_and_reorders_atomically(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    collection = catalog.create_collection("Reading list")

    catalog.add_collection_papers(collection.id, ["paper-0", "paper-1"])
    unchanged = catalog.add_collection_papers(collection.id, ["paper-1"])
    assert [item.paper_id for item in unchanged.papers] == ["paper-0", "paper-1"]

    reordered = catalog.reorder_collection(collection.id, ["paper-1", "paper-0"])
    assert [(item.paper_id, item.position) for item in reordered.papers] == [
        ("paper-1", 0),
        ("paper-0", 1),
    ]
    with pytest.raises(CatalogValidationError):
        catalog.reorder_collection(collection.id, ["paper-0"])
    assert [item.paper_id for item in catalog.get_collection(collection.id).papers] == [
        "paper-1",
        "paper-0",
    ]


def test_papers_can_be_filtered_to_unfiled_members(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    collection = catalog.create_collection("Reading list")
    catalog.add_collection_papers(collection.id, ["paper-0", "paper-1"])

    page = catalog.list_papers(PaperFilters(unfiled=True))

    assert {paper.id for paper in page.items} == {"paper-2"}


def test_collection_members_can_be_listed_in_collection_order(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    collection = catalog.create_collection("Reading list")
    catalog.add_collection_papers(collection.id, ["paper-2", "paper-0"])

    page = catalog.list_papers(
        PaperFilters(collection_id=collection.id),
        sort=PaperSort.COLLECTION_ORDER,
        direction=SortDirection.ASC,
    )

    assert [paper.id for paper in page.items] == ["paper-2", "paper-0"]


def test_removing_membership_compacts_positions_without_deleting_paper(tmp_path: Path) -> None:
    catalog, _ = _library(tmp_path)
    collection = catalog.create_collection("Queue")
    catalog.add_collection_papers(collection.id, ["paper-0", "paper-1", "paper-2"])

    result = catalog.remove_collection_paper(collection.id, "paper-1")

    assert [(item.paper_id, item.position) for item in result.papers] == [
        ("paper-0", 0),
        ("paper-2", 1),
    ]
    assert catalog.get_paper("paper-1").id == "paper-1"


def test_paper_delete_cascades_organization_records(tmp_path: Path) -> None:
    catalog, database_path = _library(tmp_path)
    tag = catalog.create_tag("Keep")
    collection = catalog.create_collection("Queue")
    catalog.set_paper_tags("paper-0", [tag.id])
    catalog.add_collection_papers(collection.id, ["paper-0"])

    with connect_database(database_path) as connection:
        connection.execute("DELETE FROM papers WHERE id = ?", ("paper-0",))
        assert connection.execute("SELECT COUNT(*) FROM paper_tags").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM collection_papers").fetchone()[0] == 0


def test_user_metadata_survives_pipeline_refresh(tmp_path: Path) -> None:
    catalog, database_path = _library(tmp_path, 1)
    original = catalog.get_paper("paper-0")
    edited = catalog.update_user_metadata(
        "paper-0",
        title="My title",
        abstract="My abstract",
        year=2025,
        expected_updated_at=original.updated_at,
    )
    assert edited.metadata_sources == {"abstract": "user", "title": "user", "year": "user"}

    update_paper_metadata(
        database_path,
        "paper-0",
        BibliographicMetadata(
            title="Generated title",
            abstract="Generated abstract",
            year=2024,
            venue="Generated venue",
            sources={
                "title": "crossref",
                "abstract": "crossref",
                "year": "crossref",
                "venue": "crossref",
            },
        ),
        PaperStatus.METADATA_RESOLVED,
    )

    refreshed = catalog.get_paper("paper-0")
    assert (refreshed.title, refreshed.abstract, refreshed.year, refreshed.venue) == (
        "My title",
        "My abstract",
        2025,
        "Generated venue",
    )
    assert refreshed.metadata_sources == {
        "title": "user",
        "abstract": "user",
        "venue": "crossref",
        "year": "user",
    }
    with pytest.raises(CatalogConflictError):
        catalog.update_user_metadata(
            "paper-0", title="Stale edit", expected_updated_at=original.updated_at
        )


def test_metadata_refresh_without_abstract_preserves_parser_abstract(tmp_path: Path) -> None:
    catalog, database_path = _library(tmp_path, 1)
    update_paper_abstract(
        database_path,
        "paper-0",
        "An abstract extracted from the managed PDF.",
        source="pdf",
    )

    update_paper_metadata(
        database_path,
        "paper-0",
        BibliographicMetadata(
            title="Refreshed title",
            sources={"title": "crossref"},
        ),
        PaperStatus.METADATA_RESOLVED,
    )

    refreshed = catalog.get_paper("paper-0")
    assert refreshed.abstract == "An abstract extracted from the managed PDF."
    assert refreshed.metadata_sources["abstract"] == "pdf"


def test_artifact_resolution_stays_inside_data_directory(tmp_path: Path) -> None:
    catalog, database_path = _library(tmp_path, 1)
    artifact = tmp_path / "artifacts" / "summary.json"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO artifacts (id, paper_id, kind, path, size_bytes)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("safe", "paper-0", "summary_json", "artifacts/summary.json", 2),
        )
    assert catalog.resolve_artifact("paper-0", "summary_json") == artifact

    with connect_database(database_path) as connection:
        connection.execute("UPDATE artifacts SET path = '../secret' WHERE id = 'safe'")
    with pytest.raises(InvalidArtifactError, match="escapes"):
        catalog.resolve_artifact("paper-0", "summary_json")
    with pytest.raises(CatalogNotFoundError):
        catalog.resolve_artifact("paper-0", "outline_md")


def test_summary_validation_uses_the_pipeline_schema() -> None:
    content = '{"schema_version":"2","identity":{"title":"Paper"}}'

    assert validate_summary_json(content)["identity"] == {
        "title": "Paper",
        "authors": [],
        "year": None,
        "venue": None,
        "doi": None,
        "arxiv_id": None,
    }
    with pytest.raises(InvalidArtifactError, match="supported schema"):
        validate_summary_json('{"schema_version":"1"}')
