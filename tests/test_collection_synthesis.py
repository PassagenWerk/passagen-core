import hashlib
import json
from pathlib import Path

import pytest

from passagen.assistant.errors import AnswerValidationError, ScopeError
from passagen.assistant.schemas import source_fingerprint
from passagen.assistant.snapshots import build_collection_snapshot
from passagen.catalog import CatalogService
from passagen.config import AssistantSettings, LlmProfileSettings, LlmSettings
from passagen.external.llm import LlmResponse
from passagen.research import CollectionSynthesisService, render_synthesis_markdown
from passagen.stages.summarization.schema import StructuredSummary
from passagen.storage.database import connect_database, initialize_database


class SynthesisProvider:
    provider_name = "fake"
    model = "fake-collection"

    def __init__(self, *, bad_calls: int = 0) -> None:
        self.prompts: list[str] = []
        self.max_tokens: list[int] = []
        self.bad_calls = bad_calls

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        self.prompts.append(prompt)
        self.max_tokens.append(max_tokens)
        source_text = prompt.split("SOURCES:\n", 1)[1] if "SOURCES:\n" in prompt else "[]"
        sources, _end = json.JSONDecoder().raw_decode(source_text)
        citations = _source_citations(sources)
        if self.bad_calls:
            self.bad_calls -= 1
            citations[0]["artifact_sha256"] = "0" * 64
        paper_ids = list(dict.fromkeys(str(item["paper_id"]) for item in citations))
        citation_models = []
        for index, item in enumerate(citations):
            citation_models.append(
                {
                    "citation_id": f"c-{index}",
                    "paper_id": item["paper_id"],
                    "artifact_kind": "summary_json",
                    "artifact_id": item["artifact_id"],
                    "artifact_sha256": item["artifact_sha256"],
                    "summary_path": "identity.title",
                }
            )
        by_paper = {citation["paper_id"]: citation["citation_id"] for citation in citation_models}
        content = {
            "schema_version": "1",
            "overview": "The collection studies related systems.",
            "themes": [
                {
                    "name": "Systems",
                    "description": "The papers study systems.",
                    "paper_ids": paper_ids,
                    "citation_ids": list(by_paper.values()),
                }
            ],
            "comparison_matrix": {
                "dimensions": ["Focus"],
                "rows": [
                    {
                        "paper_id": paper_id,
                        "cells": [
                            {
                                "dimension": "Focus",
                                "value": "A systems contribution",
                                "citation_ids": [by_paper[paper_id]],
                            }
                        ],
                    }
                    for paper_id in paper_ids
                ],
            },
            "claims": [
                {
                    "text": "The collection contains systems research.",
                    "citation_ids": list(by_paper.values()),
                }
            ],
            "citations": citation_models,
            "coverage": {
                "included_paper_ids": paper_ids,
                "missing_summary_paper_ids": [],
                "partial": False,
            },
        }
        return LlmResponse(
            content=json.dumps(content), input_tokens=100, output_tokens=50, finish_reason="stop"
        )


def _source_citations(node: object) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if isinstance(node, list):
        for value in node:
            found.extend(_source_citations(value))
    elif isinstance(node, dict):
        if {"paper_id", "artifact_id", "artifact_sha256"} <= node.keys():
            found.append(
                {
                    "paper_id": str(node["paper_id"]),
                    "artifact_id": str(node["artifact_id"]),
                    "artifact_sha256": str(node["artifact_sha256"]),
                }
            )
        else:
            for value in node.values():
                found.extend(_source_citations(value))
    return list({item["paper_id"]: item for item in found}.values())


def _environment(tmp_path: Path, *, papers: int = 2, summary_size: int = 0):
    database_path = tmp_path / "passagen.db"
    initialize_database(database_path)
    catalog = CatalogService(database_path, tmp_path)
    paper_ids = [f"paper-{index}" for index in range(papers)]
    with connect_database(database_path) as connection:
        for index, paper_id in enumerate(paper_ids):
            connection.execute(
                """
                INSERT INTO papers
                    (id, title, original_filename, pdf_sha256, status)
                VALUES (?, ?, ?, ?, 'summarized')
                """,
                (paper_id, f"Paper {index}", f"{paper_id}.pdf", f"{index:x}".zfill(64)),
            )
            summary = StructuredSummary.model_validate(
                {
                    "identity": {"title": f"Paper {index}"},
                    "problem": {"context": "x" * summary_size},
                }
            )
            content = (summary.model_dump_json(indent=2) + "\n").encode()
            path = tmp_path / "papers" / paper_id / "summary.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            connection.execute(
                """
                INSERT INTO artifacts (id, paper_id, kind, path, version, sha256, size_bytes)
                VALUES (?, ?, 'summary_json', ?, '2', ?, ?)
                """,
                (
                    f"summary-{index}",
                    paper_id,
                    path.relative_to(tmp_path).as_posix(),
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                ),
            )
    collection = catalog.create_collection("Ordered papers")
    catalog.add_collection_papers(collection.id, paper_ids)
    return database_path, catalog, collection.id, paper_ids


def _service(
    tmp_path: Path,
    database_path: Path,
    provider: SynthesisProvider,
    *,
    context: int = 100_000,
    input_limit: int = 64_000,
) -> CollectionSynthesisService:
    return CollectionSynthesisService(
        database_path,
        tmp_path,
        LlmSettings(default=LlmProfileSettings(max_context_window=context)),
        AssistantSettings(
            collection_max_input_tokens=input_limit,
            collection_map_max_output_tokens=100,
            collection_synthesis_max_output_tokens=100,
        ),
        provider=provider,
    )


def test_collection_snapshot_fingerprint_preserves_order_and_summary_hash(tmp_path: Path) -> None:
    database_path, catalog, collection_id, paper_ids = _environment(tmp_path)
    original = build_collection_snapshot(database_path, tmp_path, collection_id)

    catalog.reorder_collection(collection_id, list(reversed(paper_ids)))
    reordered = build_collection_snapshot(database_path, tmp_path, collection_id)

    assert original.paper_ids() == tuple(paper_ids)
    assert reordered.paper_ids() == tuple(reversed(paper_ids))
    assert source_fingerprint(original) != source_fingerprint(reordered)
    assert original.collection is not None
    assert [artifact.kind for artifact in original.collection.papers[0].artifacts] == [
        "summary_json"
    ]


def test_missing_summary_is_rejected_by_default_and_partial_is_explicit(tmp_path: Path) -> None:
    database_path, _catalog, collection_id, paper_ids = _environment(tmp_path)
    with connect_database(database_path) as connection:
        connection.execute("DELETE FROM artifacts WHERE paper_id = ?", (paper_ids[-1],))
    provider = SynthesisProvider()
    service = _service(tmp_path, database_path, provider)

    with pytest.raises(ScopeError, match="allow_partial=True"):
        service.synthesize(collection_id)
    result = service.synthesize(collection_id, allow_partial=True)

    assert result.synthesis.coverage.partial
    assert result.synthesis.coverage.missing_summary_paper_ids == [paper_ids[-1]]
    assert result.synthesis.coverage.included_paper_ids == [paper_ids[0]]


def test_direct_synthesis_persists_immutable_bundle_reuses_and_forces(tmp_path: Path) -> None:
    database_path, _catalog, collection_id, _paper_ids = _environment(tmp_path)
    provider = SynthesisProvider()
    service = _service(tmp_path, database_path, provider)

    generated = service.synthesize(collection_id)
    reused = service.synthesize(collection_id)
    forced = service.synthesize(collection_id, force=True)

    assert generated.strategy == "direct"
    assert generated.disposition == "generated"
    assert reused.disposition == "reused"
    assert reused.run_id == generated.run_id
    assert forced.run_id != generated.run_id
    assert len(provider.prompts) == 2
    assert {artifact.kind for artifact in generated.artifacts} == {
        "synthesis_json",
        "synthesis_markdown",
        "synthesis_source",
    }
    for artifact in generated.artifacts:
        path = tmp_path / artifact.path
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256
    assert render_synthesis_markdown(generated.synthesis).startswith("# Collection Synthesis\n")
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM generation_llm_calls").fetchone()[0] == 2


def test_old_synthesis_becomes_stale_after_summary_change(tmp_path: Path) -> None:
    database_path, _catalog, collection_id, paper_ids = _environment(tmp_path)
    service = _service(tmp_path, database_path, SynthesisProvider())
    generated = service.synthesize(collection_id)
    with connect_database(database_path) as connection:
        connection.execute(
            "UPDATE artifacts SET sha256 = ? WHERE paper_id = ? AND kind = 'summary_json'",
            ("f" * 64, paper_ids[0]),
        )

    latest = service.latest(collection_id)

    assert latest is not None
    assert latest.source_status.stale
    assert "summary_content_changed" in latest.source_status.reasons
    assert not generated.source_status.stale


def test_invalid_cross_paper_citation_gets_one_repair(tmp_path: Path) -> None:
    database_path, _catalog, collection_id, _paper_ids = _environment(tmp_path)
    provider = SynthesisProvider(bad_calls=1)
    result = _service(tmp_path, database_path, provider).synthesize(collection_id)

    assert result.synthesis.citations[0].artifact_sha256 != "0" * 64
    with connect_database(database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute("SELECT stage FROM generation_llm_calls ORDER BY rowid")
        ]
    assert stages == ["reduce", "repair"]


def test_unrepairable_synthesis_fails_without_product_artifacts(tmp_path: Path) -> None:
    database_path, _catalog, collection_id, _paper_ids = _environment(tmp_path)
    provider = SynthesisProvider(bad_calls=2)
    service = _service(tmp_path, database_path, provider)

    with pytest.raises(AnswerValidationError):
        service.synthesize(collection_id)

    with connect_database(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM collection_artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM generation_runs").fetchone()[0] == "failed"


def test_large_collection_uses_bounded_map_reduce_and_accounts_calls(tmp_path: Path) -> None:
    database_path, _catalog, collection_id, _paper_ids = _environment(
        tmp_path, papers=4, summary_size=8_000
    )
    provider = SynthesisProvider()
    service = _service(tmp_path, database_path, provider, context=12_000, input_limit=5_000)

    result = service.synthesize(collection_id)

    assert result.strategy == "map_reduce"
    supplied = [
        json.JSONDecoder().raw_decode(prompt.split("SOURCES:\n", 1)[1])[0]
        for prompt in provider.prompts
    ]
    assert all("extracted_json" not in json.dumps(documents) for documents in supplied)
    assert all(limit == 100 for limit in provider.max_tokens)
    with connect_database(database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute("SELECT stage FROM generation_llm_calls ORDER BY rowid")
        ]
    assert stages.count("map") >= 2
    assert stages[-1] == "reduce"
