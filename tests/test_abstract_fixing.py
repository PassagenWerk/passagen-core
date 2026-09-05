import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from passagen.config import AbstractFixingSettings, LlmSettings, PipelineSettings, ProvidersSettings
from passagen.domain import Paper, PaperStatus
from passagen.providers import (
    LlmCallStats,
    LlmResponse,
    LlmStage,
    ProviderHealthSnapshot,
    ProviderStatus,
)
from passagen.stages.abstract_fixing import (
    AbstractFixError,
    fix_paper_abstract,
    load_cleaned_abstract,
)
from passagen.stages.updating import update_papers
from passagen.storage.database import initialize_database
from passagen.storage.repository import (
    get_paper,
    register_pdf,
    update_paper_abstract,
    update_paper_status,
)


class FakeProvider:
    provider_name = "fake"
    model = "test-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        assert "Raw extracted abstract" in prompt
        assert max_tokens == 2_000
        self.calls += 1
        return LlmResponse(self.responses.pop(0), input_tokens=20, output_tokens=10)


def setup_abstract(tmp_path: Path, abstract: str) -> tuple[Path, Path, str]:
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    initialize_database(database_path)
    paper = Paper(original_filename="paper.pdf", pdf_sha256="a" * 64, file_size_bytes=1)
    register_pdf(database_path, paper, Path("pdfs/aa/paper.pdf"))
    update_paper_abstract(database_path, paper.id, abstract, source="grobid")
    return database_path, data_dir, paper.id


def test_abstract_fix_saves_validated_artifact_and_reuses_cache(tmp_path: Path) -> None:
    raw = (
        "The system supports 5↔ more features with overhead ↗0.05%. "
        "It preserves the original claims and terminology. Introduction This leaked section "
        "reports 99 unrelated examples and should be removed."
    )
    cleaned = (
        "The system supports 5× more features with overhead <0.05%. "
        "It preserves the original claims and terminology."
    )
    database_path, data_dir, paper_id = setup_abstract(tmp_path, raw)
    provider = FakeProvider(
        [json.dumps({"cleaned_abstract": cleaned, "corrections": ["Removed leaked section"]})]
    )
    stats = LlmCallStats()

    result = fix_paper_abstract(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        AbstractFixingSettings(),
        provider=provider,
        llm_stats=stats,
    )

    assert result.updated is True
    assert result.cleaned is not None
    assert result.cleaned.cleaned_abstract == cleaned
    assert result.artifact is not None
    assert (data_dir / result.artifact.path).is_file()
    stored = get_paper(database_path, paper_id)
    assert stored is not None
    assert stored.abstract == raw
    assert stats.by_stage[LlmStage.ABSTRACT].calls == 1
    assert load_cleaned_abstract(database_path, data_dir, paper_id) == result.cleaned

    cached = fix_paper_abstract(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        AbstractFixingSettings(),
        provider=provider,
    )
    assert cached.updated is False
    assert cached.cleaned == result.cleaned
    assert provider.calls == 1


def test_abstract_fix_rejects_changed_numeric_values(tmp_path: Path) -> None:
    raw = "The evaluation reports 17.2% higher throughput while preserving every original claim."
    database_path, data_dir, paper_id = setup_abstract(tmp_path, raw)
    provider = FakeProvider(
        [
            json.dumps(
                {
                    "cleaned_abstract": (
                        "The evaluation reports 17.5% higher throughput while preserving every "
                        "original claim."
                    ),
                    "corrections": [],
                }
            )
        ]
    )

    with pytest.raises(AbstractFixError, match="numeric value"):
        fix_paper_abstract(
            database_path,
            data_dir,
            paper_id,
            LlmSettings(),
            AbstractFixingSettings(),
            provider=provider,
        )

    assert load_cleaned_abstract(database_path, data_dir, paper_id) is None


def test_update_pipeline_runs_abstract_fix_before_outline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "A raw author abstract with enough text to pass conservative validation checks."
    cleaned = "A clean author abstract with enough text to pass conservative validation checks."
    database_path, data_dir, paper_id = setup_abstract(tmp_path, raw)
    update_paper_status(database_path, paper_id, PaperStatus.SUMMARIZED)
    provider = FakeProvider(
        [json.dumps({"cleaned_abstract": cleaned, "corrections": ["Repaired wording"]})]
    )
    events: list[str] = []

    def fake_outline(*_args: object, **_kwargs: object) -> SimpleNamespace:
        paper = get_paper(database_path, paper_id)
        assert paper is not None
        return SimpleNamespace(paper=paper)

    monkeypatch.setattr("passagen.stages.updating.service.outline_paper", fake_outline)

    result = update_papers(
        database_path,
        data_dir,
        ProvidersSettings(),
        PipelineSettings(),
        paper_ids=[paper_id],
        abstract_provider=provider,
        provider_health=ProviderHealthSnapshot({"llm": ProviderStatus("llm", True, "test")}),
        on_event=lambda event: events.append(event.stage),
    )

    assert not result.failures
    assert provider.calls == 1
    assert load_cleaned_abstract(database_path, data_dir, paper_id) is not None
    assert events.index("abstract clean") < events.index("outline")


def test_update_pipeline_reports_abstract_clean_failure_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "The evaluation reports 17.2% higher throughput while preserving every original claim."
    database_path, data_dir, paper_id = setup_abstract(tmp_path, raw)
    update_paper_status(database_path, paper_id, PaperStatus.SUMMARIZED)
    provider = FakeProvider(
        [json.dumps({"cleaned_abstract": raw.replace("17.2", "17.5"), "corrections": []})]
    )

    def fake_outline(*_args: object, **_kwargs: object) -> SimpleNamespace:
        paper = get_paper(database_path, paper_id)
        assert paper is not None
        return SimpleNamespace(paper=paper)

    monkeypatch.setattr("passagen.stages.updating.service.outline_paper", fake_outline)

    result = update_papers(
        database_path,
        data_dir,
        ProvidersSettings(),
        PipelineSettings(),
        paper_ids=[paper_id],
        abstract_provider=provider,
        provider_health=ProviderHealthSnapshot({"llm": ProviderStatus("llm", True, "test")}),
    )

    assert not result.failures
    assert len(result.warnings) == 1
    assert "numeric value" in result.warnings[0].message
    assert [paper.id for paper in result.updated] == [paper_id]


def test_rebuild_abstract_only_does_not_regenerate_downstream_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "A raw author abstract with enough text to pass conservative validation checks."
    cleaned = "A clean author abstract with enough text to pass conservative validation checks."
    database_path, data_dir, paper_id = setup_abstract(tmp_path, raw)
    update_paper_status(database_path, paper_id, PaperStatus.OUTLINED)
    provider = FakeProvider(
        [json.dumps({"cleaned_abstract": cleaned, "corrections": ["Repaired wording"]})]
    )

    monkeypatch.setattr(
        "passagen.stages.updating.service.summarize_paper",
        lambda *_args, **_kwargs: pytest.fail("Summary must not run for an Abstract-only rebuild"),
    )
    monkeypatch.setattr(
        "passagen.stages.updating.service.outline_paper",
        lambda *_args, **_kwargs: pytest.fail("Outline must not run for an Abstract-only rebuild"),
    )

    result = update_papers(
        database_path,
        data_dir,
        ProvidersSettings(),
        PipelineSettings(),
        paper_ids=[paper_id],
        from_stage="abstract",
        abstract_provider=provider,
        provider_health=ProviderHealthSnapshot({"llm": ProviderStatus("llm", True, "test")}),
    )

    assert not result.failures
    assert [paper.id for paper in result.updated] == [paper_id]
    assert result.updated[0].status is PaperStatus.OUTLINED
    assert load_cleaned_abstract(database_path, data_dir, paper_id) is not None
