from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from passagen.config import Settings
from passagen.domain import Paper, PaperStatus
from passagen.parsing import ParsedPaper, ParsedSection
from passagen.processing import (
    ProcessingError,
    ProcessingService,
    RunConflictError,
    RunNotFoundError,
    RunNotQueuedError,
    RunStatus,
    UnknownPaperError,
)
from passagen.providers import LlmResponse
from passagen.storage.database import initialize_database
from passagen.storage.repository import register_pdf, save_parsed_artifact


class FakeProvider:
    provider_name = "fake"
    model = "test-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        del max_tokens
        self.prompts.append(prompt)
        return LlmResponse(self.responses.pop(0), input_tokens=10, output_tokens=5)


def valid_summary(title: str = "Test Paper") -> str:
    return json.dumps({"identity": {"title": title, "authors": []}})


def valid_outline() -> str:
    return json.dumps(
        {"introduction": {"thesis": "The paper introduces a test problem.", "points": []}}
    )


def setup_library(tmp_path: Path) -> tuple[Settings, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "passagen.db"
    initialize_database(database_path)
    settings = Settings(data_dir=data_dir, database_path=database_path)
    return settings, database_path, data_dir


def add_discovered_paper(database_path: Path, name: str = "paper.pdf") -> str:
    paper = Paper(
        original_filename=name,
        pdf_sha256=hashlib.sha256(name.encode()).hexdigest(),
        file_size_bytes=1,
    )
    register_pdf(database_path, paper, Path("pdfs") / "aa" / f"{paper.pdf_sha256}.pdf")
    return paper.id


def add_parsed_paper(database_path: Path, data_dir: Path, name: str = "paper.pdf") -> str:
    paper_id = add_discovered_paper(database_path, name)
    parsed = ParsedPaper(
        parser="test",
        sections=(ParsedSection(title="Introduction", text="A test paper.", pages=(1,)),),
    )
    relative_path = Path("papers") / paper_id / "extracted.json"
    content = (parsed.model_dump_json() + "\n").encode()
    target = data_dir / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    save_parsed_artifact(
        database_path,
        paper_id,
        relative_path,
        version=parsed.schema_version,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        status=PaperStatus.PARSED,
    )
    return paper_id


def test_start_update_persists_queued_run_with_snapshot(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    paper_id = add_discovered_paper(database_path)
    service = ProcessingService(settings)

    run = service.start_update([paper_id])

    assert run.status is RunStatus.QUEUED
    assert run.mode == "continue"
    assert run.paper_ids == [paper_id]
    persisted = service.get_run(run.id)
    assert persisted.status is RunStatus.QUEUED
    snapshot = json.loads((data_dir / "runs" / run.id / "run.json").read_text())
    assert snapshot["run_id"] == run.id
    assert snapshot["paper_ids"] == [paper_id]
    assert snapshot["config"]["providers"]["llm"]["model"]
    assert snapshot["config"]["prompts"]["summary"]
    # Only the API key environment variable *name* is recorded, never a key value.
    assert snapshot["config"]["providers"]["llm"]["api_key_env"] == "PASSAGEN_API_KEY"
    assert "api_key" not in {
        key for key in snapshot["config"]["providers"]["llm"] if key != "api_key_env"
    }


def test_start_update_rejects_conflicting_active_run(tmp_path: Path) -> None:
    settings, database_path, _ = setup_library(tmp_path)
    paper_id = add_discovered_paper(database_path)
    service = ProcessingService(settings)
    first = service.start_update([paper_id])

    with pytest.raises(RunConflictError, match=first.id):
        service.start_update([paper_id])

    other = add_discovered_paper(database_path, "other.pdf")
    second = service.start_update([other])
    assert second.id != first.id


def test_start_update_validates_papers_and_mode(tmp_path: Path) -> None:
    settings, database_path, _ = setup_library(tmp_path)
    paper_id = add_discovered_paper(database_path)
    service = ProcessingService(settings)

    with pytest.raises(UnknownPaperError, match="Paper not found: missing"):
        service.start_update(["missing"])
    with pytest.raises(ProcessingError, match="mode='rebuild'"):
        service.start_update([paper_id], from_stage="parse")
    with pytest.raises(ProcessingError, match="Unknown stage"):
        service.start_update([paper_id], mode="rebuild", from_stage="publish")


def test_execute_run_completes_and_records_events(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    paper_id = add_parsed_paper(database_path, data_dir)
    service = ProcessingService(settings)
    provider = FakeProvider([valid_summary(), valid_outline()])
    run = service.start_update([paper_id])

    result = service.execute_run(run.id, summary_provider=provider)

    assert [paper.id for paper in result.updated] == [paper_id]
    finished = service.get_run(run.id)
    assert finished.status is RunStatus.COMPLETED
    assert finished.finished_at is not None
    assert finished.result is not None
    assert finished.result.updated == [paper_id]
    assert finished.result.failed == []
    events = service.list_events(run.id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert any(event.stage == "summary" for event in events)
    assert any(event.stage == "outline" for event in events)
    incremental = service.list_events(run.id, after=events[-2].sequence)
    assert [event.sequence for event in incremental] == [events[-1].sequence]


def test_execute_run_isolates_paper_failure_with_category(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    failing = add_discovered_paper(database_path, "missing.pdf")
    succeeding = add_parsed_paper(database_path, data_dir, "parsed.pdf")
    service = ProcessingService(settings)
    provider = FakeProvider([valid_summary(), valid_outline()])
    run = service.start_update([failing, succeeding])

    result = service.execute_run(run.id, summary_provider=provider)

    assert [failure.paper_id for failure in result.failures] == [failing]
    assert result.failures[0].category == "metadata"
    assert [paper.id for paper in result.updated] == [succeeding]
    finished = service.get_run(run.id)
    assert finished.status is RunStatus.COMPLETED
    assert finished.result is not None
    assert [failure.paper_id for failure in finished.result.failed] == [failing]
    assert finished.result.failed[0].category == "metadata"


def test_execute_run_requires_queued_status(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    paper_id = add_parsed_paper(database_path, data_dir)
    service = ProcessingService(settings)
    provider = FakeProvider([valid_summary(), valid_outline()])
    run = service.start_update([paper_id])
    service.execute_run(run.id, summary_provider=provider)

    with pytest.raises(RunNotQueuedError):
        service.execute_run(run.id, summary_provider=provider)
    with pytest.raises(RunNotFoundError):
        service.execute_run("missing-run")
    with pytest.raises(RunNotFoundError):
        service.get_run("missing-run")


def test_rebuild_from_summary_reruns_llm_stages(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    paper_id = add_parsed_paper(database_path, data_dir)
    service = ProcessingService(settings)
    first = FakeProvider([valid_summary(), valid_outline()])
    service.execute_run(service.start_update([paper_id]).id, summary_provider=first)

    second = FakeProvider([valid_summary(), valid_outline()])
    run = service.start_update([paper_id], mode="rebuild", from_stage="summary")
    result = service.execute_run(run.id, summary_provider=second)

    assert [paper.id for paper in result.updated] == [paper_id]
    assert len(second.prompts) == 2
    assert service.get_run(run.id).mode == "rebuild"
    assert service.get_run(run.id).from_stage == "summary"


def test_rebuild_from_outline_reruns_only_outline(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    paper_id = add_parsed_paper(database_path, data_dir)
    service = ProcessingService(settings)
    service.execute_run(
        service.start_update([paper_id]).id,
        summary_provider=FakeProvider([valid_summary(), valid_outline()]),
    )

    provider = FakeProvider([valid_outline()])
    run = service.start_update([paper_id], mode="rebuild", from_stage="outline")
    result = service.execute_run(run.id, summary_provider=provider)

    assert [paper.id for paper in result.updated] == [paper_id]
    assert len(provider.prompts) == 1
    assert service.get_run(run.id).from_stage == "outline"


def test_continue_skips_completed_paper(tmp_path: Path) -> None:
    settings, database_path, data_dir = setup_library(tmp_path)
    paper_id = add_parsed_paper(database_path, data_dir)
    service = ProcessingService(settings)
    provider = FakeProvider([valid_summary(), valid_outline()])
    service.execute_run(service.start_update([paper_id]).id, summary_provider=provider)

    run = service.start_update([paper_id])
    result = service.execute_run(run.id, summary_provider=FakeProvider([]))

    assert [paper.id for paper in result.skipped] == [paper_id]
    assert service.get_run(run.id).result is not None
    assert service.get_run(run.id).result.skipped == [paper_id]  # type: ignore[union-attr]


def test_interrupt_active_runs_marks_queued_and_running(tmp_path: Path) -> None:
    settings, database_path, _ = setup_library(tmp_path)
    first = add_discovered_paper(database_path, "first.pdf")
    second = add_discovered_paper(database_path, "second.pdf")
    service = ProcessingService(settings)
    first_run = service.start_update([first])
    second_run = service.start_update([second])

    interrupted = service.interrupt_active_runs()

    assert {run.id for run in interrupted} == {first_run.id, second_run.id}
    for run in service.list_runs():
        assert run.status is RunStatus.INTERRUPTED
        assert run.finished_at is not None


def test_list_runs_filters_by_status_and_paper(tmp_path: Path) -> None:
    settings, database_path, _ = setup_library(tmp_path)
    first = add_discovered_paper(database_path, "first.pdf")
    second = add_discovered_paper(database_path, "second.pdf")
    service = ProcessingService(settings)
    queued = service.start_update([first])
    other = service.start_update([second])

    assert {run.id for run in service.list_runs(status="queued")} == {queued.id, other.id}
    assert [run.id for run in service.list_runs(paper_id=first)] == [queued.id]
    assert service.list_runs(status="completed") == []
