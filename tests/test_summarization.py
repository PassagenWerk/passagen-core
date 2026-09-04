from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from passagen.config import (
    LlmSettings,
    PipelineSettings,
    ProvidersSettings,
    SummarizationSettings,
    SummarizationStrategy,
)
from passagen.domain import Paper, PaperStatus
from passagen.parsing import ParsedPaper, ParsedSection
from passagen.providers import LlmCallStats, LlmResponse, LlmStage
from passagen.stages.summarization import StructuredSummary, SummaryError, summarize_paper
from passagen.stages.updating import update_papers
from passagen.storage.database import connect_database, initialize_database
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


def setup_parsed_paper(
    tmp_path: Path, sections: tuple[ParsedSection, ...] | None = None
) -> tuple[Path, Path, str]:
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    initialize_database(database_path)
    paper = Paper(original_filename="paper.pdf", pdf_sha256="a" * 64, file_size_bytes=1)
    register_pdf(database_path, paper, Path("pdfs/aa/paper.pdf"))
    parsed = ParsedPaper(
        parser="test",
        sections=sections
        or (ParsedSection(title="Introduction", text="A test paper.", pages=(1,)),),
    )
    relative_path = Path("papers") / paper.id / "extracted.json"
    content = (parsed.model_dump_json() + "\n").encode()
    target = data_dir / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    save_parsed_artifact(
        database_path,
        paper.id,
        relative_path,
        version=parsed.schema_version,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        status=PaperStatus.PARSED,
    )
    return database_path, data_dir, paper.id


def valid_summary(title: str = "Test Paper") -> str:
    return json.dumps({"identity": {"title": title, "authors": []}})


def valid_outline() -> str:
    return json.dumps(
        {
            "introduction": {
                "thesis": "The paper introduces a test problem.",
                "points": [],
            }
        }
    )


def hierarchical_settings() -> SummarizationSettings:
    return SummarizationSettings(strategy=SummarizationStrategy.HIERARCHICAL)


def test_summary_v2_separates_subject_and_baseline_values() -> None:
    summary = StructuredSummary.model_validate(
        {
            "identity": {"title": "Test", "authors": []},
            "evaluation": {
                "results": [
                    {
                        "metric": "latency",
                        "metric_direction": "lower_is_better",
                        "subject": "New system",
                        "subject_value": "1 ms",
                        "baseline": "Baseline",
                        "baseline_value": "2 ms",
                        "improvement": "50% lower latency",
                        "evidence_pages": [7],
                    }
                ]
            },
        }
    )

    assert summary.schema_version == "2"
    assert summary.evaluation.results[0].subject_value == "1 ms"
    assert summary.evaluation.results[0].baseline_value == "2 ms"
    with pytest.raises(ValidationError):
        StructuredSummary.model_validate(
            {
                "identity": {"title": "Test", "authors": []},
                "evaluation": {
                    "key_results": [{"claim": "Ambiguous comparison", "value": "1 ms vs 2 ms"}]
                },
            }
        )


def test_summarize_full_mode_saves_validated_json_yaml_and_call_audit(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    provider = FakeProvider([valid_summary()])
    stats = LlmCallStats()

    result = summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        SummarizationSettings(),
        provider=provider,
        execution_log_dir=tmp_path / "logs" / "run",
        llm_stats=stats,
    )

    assert result.updated is True
    assert result.paper.status is PaperStatus.SUMMARIZED
    assert result.summary is not None
    assert result.summary.identity.title == "paper.pdf"
    assert (data_dir / "papers" / paper_id / "summary.json").is_file()
    assert (data_dir / "papers" / paper_id / "summary.yaml").is_file()
    call_dir = tmp_path / "logs" / "run" / "external" / "llm" / paper_id
    full_call = json.loads((call_dir / "summary-full.json").read_text(encoding="utf-8"))
    assert "Complete serialized paper" in full_call["prompt"]
    assert "A test paper." in full_call["prompt"]
    assert full_call["max_tokens"] == 3000
    assert stats.by_stage[LlmStage.EVIDENCE].calls == 0
    assert stats.by_stage[LlmStage.SUMMARY].calls == 1
    assert stats.total.total_tokens == 15
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM processing_runs").fetchone()[0] == "completed"


def test_auto_strategy_falls_back_to_hierarchical_when_full_prompt_exceeds_budget(
    tmp_path: Path,
) -> None:
    big_text = ("A detailed paragraph about the system design. " * 300).strip()
    database_path, data_dir, paper_id = setup_parsed_paper(
        tmp_path,
        sections=(ParsedSection(title="Design", text=big_text, pages=(3, 4)),),
    )
    settings = LlmSettings(
        context_window_tokens=15_000,
        max_context_utilization=1.0,
        safety_margin_tokens=1_000,
        chars_per_token=1.0,
    )
    summarization = SummarizationSettings(summary_max_output_tokens=100)
    provider = FakeProvider(['{"evidence": []}', valid_summary()])

    result = summarize_paper(
        database_path,
        data_dir,
        paper_id,
        settings,
        summarization,
        provider=provider,
        execution_log_dir=tmp_path / "logs" / "run",
    )

    assert result.updated is True
    assert len(provider.prompts) == 2
    assert "<source>" in provider.prompts[0]
    call_dir = tmp_path / "logs" / "run" / "external" / "llm" / paper_id
    assert (call_dir / "evidence-001.json").is_file()
    assert (call_dir / "summary.json").is_file()


def test_forced_full_strategy_fails_before_calling_provider_when_over_budget(
    tmp_path: Path,
) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    settings = LlmSettings(
        context_window_tokens=1_000,
        max_context_utilization=1.0,
        safety_margin_tokens=0,
        chars_per_token=1.0,
    )
    summarization = SummarizationSettings(
        strategy=SummarizationStrategy.FULL, summary_max_output_tokens=100
    )
    provider = FakeProvider([])

    with pytest.raises(SummaryError, match="exceeds the context budget"):
        summarize_paper(
            database_path, data_dir, paper_id, settings, summarization, provider=provider
        )

    assert provider.prompts == []
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT status FROM processing_runs").fetchone()[0] == "failed"


def test_summarize_hierarchical_extracts_evidence_per_chunk(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    evidence = json.dumps(
        {
            "evidence": [
                {
                    "category": "evaluation_result",
                    "claim": "System A processes 10k requests/s.",
                    "section": "Introduction",
                    "evidence_pages": [1],
                    "subject": "System A",
                    "subject_value": "10k requests/s",
                }
            ]
        }
    )
    provider = FakeProvider([evidence, valid_summary()])
    stats = LlmCallStats()

    result = summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        hierarchical_settings(),
        provider=provider,
        execution_log_dir=tmp_path / "logs" / "run",
        llm_stats=stats,
    )

    assert result.updated is True
    assert stats.by_stage[LlmStage.EVIDENCE].calls == 1
    assert stats.by_stage[LlmStage.SUMMARY].calls == 1
    evidence_prompt, summary_prompt = provider.prompts
    assert "Paper: Untitled" in evidence_prompt
    assert "Sections: Introduction" in evidence_prompt
    assert "Pages: [1]" in evidence_prompt
    assert "System A processes 10k requests/s." in summary_prompt
    call_dir = tmp_path / "logs" / "run" / "external" / "llm" / paper_id
    evidence_call = json.loads((call_dir / "evidence-001.json").read_text(encoding="utf-8"))
    assert evidence_call["response"] == evidence
    assert evidence_call["max_tokens"] == 1500


def test_summarize_locally_removes_json_code_fence(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    provider = FakeProvider([f"```json\n{valid_summary()}\n```"])

    result = summarize_paper(
        database_path, data_dir, paper_id, LlmSettings(), SummarizationSettings(), provider=provider
    )

    assert result.updated is True
    assert len(provider.prompts) == 1


def test_summarize_uses_llm_repair_for_schema_error(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    provider = FakeProvider(['{"identity": {"title": 1}}', valid_summary()])

    result = summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        SummarizationSettings(),
        provider=provider,
        execution_log_dir=tmp_path / "logs" / "run",
    )

    assert result.updated is True
    assert len(provider.prompts) == 2
    assert (tmp_path / "logs" / "run" / "external" / "llm" / paper_id / "repair-1.json").is_file()


def test_summarize_keeps_raw_response_when_repair_fails(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    provider = FakeProvider(["not json", "still not json", "also not json"])

    with pytest.raises(SummaryError, match="schema validation"):
        summarize_paper(
            database_path,
            data_dir,
            paper_id,
            LlmSettings(),
            SummarizationSettings(),
            provider=provider,
            execution_log_dir=tmp_path / "logs" / "run",
        )

    call_dir = tmp_path / "logs" / "run" / "external" / "llm" / paper_id
    assert json.loads((call_dir / "summary-full.json").read_text(encoding="utf-8"))["response"] == (
        "not json"
    )
    assert (call_dir / "repair-2.json").is_file()
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT status FROM processing_runs").fetchone()[0] == "failed"
        assert connection.execute("SELECT status FROM papers").fetchone()[0] == "parsed"


def test_summarize_retries_truncated_evidence_with_more_output_tokens(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)

    class TruncatingProvider(FakeProvider):
        def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
            self.prompts.append(prompt)
            content = self.responses.pop(0)
            truncated = len(self.prompts) == 1
            return LlmResponse(
                content,
                input_tokens=10,
                output_tokens=5,
                finish_reason="length" if truncated else "stop",
            )

    provider = TruncatingProvider(
        ['{"evidence": [{"category": "truncat', '{"evidence": []}', valid_summary()]
    )

    result = summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        hierarchical_settings(),
        provider=provider,
        execution_log_dir=tmp_path / "logs" / "run",
    )

    assert result.updated is True
    assert len(provider.prompts) == 3
    call_dir = tmp_path / "logs" / "run" / "external" / "llm" / paper_id
    assert (call_dir / "evidence-001.json").is_file()
    retry_call = json.loads((call_dir / "evidence-001-retry-2.json").read_text(encoding="utf-8"))
    assert retry_call["max_tokens"] == 3000


def test_summarize_fails_when_evidence_stays_truncated(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)

    class AlwaysTruncatingProvider(FakeProvider):
        def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
            self.prompts.append(prompt)
            return LlmResponse(
                '{"evidence": [{"category": "truncat',
                input_tokens=10,
                output_tokens=5,
                finish_reason="length",
            )

    with pytest.raises(SummaryError, match="Extracted evidence failed schema validation"):
        summarize_paper(
            database_path,
            data_dir,
            paper_id,
            LlmSettings(),
            hierarchical_settings(),
            provider=AlwaysTruncatingProvider([]),
            execution_log_dir=tmp_path / "logs" / "run",
        )

    call_dir = tmp_path / "logs" / "run" / "external" / "llm" / paper_id
    assert (call_dir / "evidence-001-retry-3.json").is_file()
    assert json.loads((call_dir / "evidence-001-retry-3.json").read_text())["max_tokens"] == 6000


def test_summarize_reuses_successful_chunk_evidence_when_forced(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        hierarchical_settings(),
        provider=FakeProvider(['{"evidence": []}', valid_summary()]),
    )
    provider = FakeProvider([valid_summary("Rebuilt")])

    result = summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        hierarchical_settings(),
        force=True,
        provider=provider,
    )

    assert result.summary is not None
    assert result.summary.identity.title == "paper.pdf"
    assert len(provider.prompts) == 1


def test_forced_summary_failure_stops_at_parsed(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    summarize_paper(
        database_path,
        data_dir,
        paper_id,
        LlmSettings(),
        SummarizationSettings(),
        provider=FakeProvider([valid_summary()]),
    )

    with pytest.raises(SummaryError):
        summarize_paper(
            database_path,
            data_dir,
            paper_id,
            LlmSettings(),
            SummarizationSettings(),
            force=True,
            provider=FakeProvider(["invalid", "invalid", "invalid"]),
        )

    with connect_database(database_path) as connection:
        assert connection.execute("SELECT status FROM papers").fetchone()[0] == "parsed"


def test_interrupted_summary_keeps_last_successful_status(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)

    class InterruptingProvider(FakeProvider):
        def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
            del prompt, max_tokens
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        summarize_paper(
            database_path,
            data_dir,
            paper_id,
            LlmSettings(),
            SummarizationSettings(),
            provider=InterruptingProvider([]),
        )

    with connect_database(database_path) as connection:
        assert connection.execute("SELECT status FROM papers").fetchone()[0] == "parsed"
        run = connection.execute("SELECT status, error_message FROM processing_runs").fetchone()
        assert tuple(run) == ("failed", "interrupted")


def test_update_advances_parsed_paper_to_outline_when_llm_is_enabled(tmp_path: Path) -> None:
    database_path, data_dir, paper_id = setup_parsed_paper(tmp_path)
    provider = FakeProvider([valid_summary(), valid_outline()])

    result = update_papers(
        database_path,
        data_dir,
        ProvidersSettings(),
        PipelineSettings(),
        summary_provider=provider,
    )

    assert result.target_status is PaperStatus.OUTLINED
    assert [paper.id for paper in result.updated] == [paper_id]
    assert result.updated[0].status is PaperStatus.OUTLINED
    assert len(provider.prompts) == 2
