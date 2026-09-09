from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from collection_support import (
    CollectionEnv,
    collection_env,
    collection_responder,
    summary_citation,
)
from support import FakeProvider

from passagen.assistant.errors import (
    AnswerValidationError,
    ScopeError,
    StaleSourceError,
)
from passagen.assistant.models import AssistantTurn
from passagen.config import LlmSettings
from passagen.generation import GenerationRunDispatcher
from passagen.research import (
    CollectionReport,
    CollectionReportResult,
    CollectionReportService,
    CollectionSynthesisService,
    ReportKind,
    render_report_markdown,
)
from passagen.storage.database import connect_database


def _report_payload(env: CollectionEnv, *, bad_sha: bool = False) -> str:
    citations = [
        summary_citation(env, "paper-a", "c-1"),
        summary_citation(env, "paper-b", "c-2"),
    ]
    if bad_sha:
        citations[0]["artifact_sha256"] = "0" * 64
    return json.dumps(
        {
            "schema_version": "1",
            "kind": "review",
            "title": "Generated title",
            "user_prompt": None,
            "synthesis_artifact_id": None,
            "coverage": {
                "included_paper_ids": list(env.paper_ids),
                "missing_summary_paper_ids": [],
                "partial": False,
            },
            "sections": [
                {
                    "heading": "Overview",
                    "body_markdown": "The papers study systems [c-1] and compilers [c-2].",
                    "claims": [
                        {"text": "The papers study systems.", "citation_ids": ["c-1", "c-2"]}
                    ],
                }
            ],
            "claims": [{"text": "The collection covers systems.", "citation_ids": ["c-1", "c-2"]}],
            "citations": citations,
        }
    )


def _report_provider(env: CollectionEnv, *, bad_calls: int = 0) -> FakeProvider:
    state = {"bad": bad_calls}

    def respond(prompt: str) -> object:
        if state["bad"]:
            state["bad"] -= 1
            return _report_payload(env, bad_sha=True)
        return _report_payload(env)

    return FakeProvider(respond)


def _service(env: CollectionEnv, provider: FakeProvider) -> CollectionReportService:
    return CollectionReportService(
        env.database_path, env.data_dir, LlmSettings(), provider=provider
    )


def _rewrite_summary(env: CollectionEnv, paper_id: str, value: str = "99 ms") -> None:
    path = env.data_dir / "papers" / paper_id / "summary.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    content["evaluation"]["results"][0]["subject_value"] = value
    updated = json.dumps(content, ensure_ascii=False)
    path.write_text(updated, encoding="utf-8")
    sha = hashlib.sha256(updated.encode()).hexdigest()
    with connect_database(env.database_path) as connection:
        connection.execute(
            "UPDATE artifacts SET sha256 = ? WHERE id = ?",
            (sha, env.artifact_ids[paper_id]["summary_json"]),
        )
    env.artifact_shas[paper_id]["summary_json"] = sha


def _drop_summary(env: CollectionEnv, paper_id: str) -> None:
    with connect_database(env.database_path) as connection:
        connection.execute(
            "DELETE FROM artifacts WHERE id = ?",
            (env.artifact_ids[paper_id]["summary_json"],),
        )


def test_create_review_report_persists_artifacts_and_run(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env)
    service = _service(env, provider)

    result = service.create_report(env.collection_id, ReportKind.REVIEW)

    assert result.disposition == "generated"
    assert result.record.status == "completed"
    assert result.record.kind is ReportKind.REVIEW
    assert result.record.title == "Literature review: Systems reading list"
    assert result.record.run_id is not None
    assert result.record.report_artifact_id is not None
    report = result.report
    assert report.schema_version == "1"
    assert report.coverage.included_paper_ids == list(env.paper_ids)
    assert report.synthesis_artifact_id is None
    kinds = {artifact.kind for artifact in result.artifacts}
    assert kinds == {"report_json", "report_markdown", "report_source", "report_input"}
    for artifact in result.artifacts:
        content = (env.data_dir / artifact.path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == artifact.sha256
    report_json = next(a for a in result.artifacts if a.kind == "report_json")
    loaded = CollectionReport.model_validate_json((env.data_dir / report_json.path).read_bytes())
    assert loaded == report
    markdown = render_report_markdown(report)
    assert "## Citations" in markdown
    assert "paper-a" in markdown
    assert result.source_status.stale is False
    with connect_database(env.database_path) as connection:
        run_row = connection.execute(
            "SELECT kind, status FROM generation_runs WHERE id = ?",
            (result.record.run_id,),
        ).fetchone()
        assert tuple(run_row) == ("report", "completed")
        call_rows = connection.execute(
            "SELECT stage, prompt_version, schema_version FROM generation_llm_calls "
            "WHERE generation_run_id = ?",
            (result.record.run_id,),
        ).fetchall()
    assert [tuple(row) for row in call_rows] == [("answer", "1", "1")]


def test_report_kinds_and_custom_prompt_rules(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env)
    service = _service(env, provider)

    for kind in (ReportKind.REVIEW, ReportKind.COMPARISON, ReportKind.GAPS):
        result = service.create_report(env.collection_id, kind)
        assert result.record.kind is kind
    custom = service.create_report(
        env.collection_id, ReportKind.CUSTOM, user_prompt="哪篇论文延迟最低？"
    )
    assert custom.record.user_prompt == "哪篇论文延迟最低？"
    assert "哪篇论文延迟最低？" in custom.report.title

    with pytest.raises(ScopeError):
        service.create_report(env.collection_id, ReportKind.CUSTOM, user_prompt="  ")
    with pytest.raises(ScopeError):
        service.create_report(env.collection_id, ReportKind.REVIEW, user_prompt="unexpected")


def test_completed_report_is_reused_until_forced(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env)
    service = _service(env, provider)

    first = service.create_report(env.collection_id, ReportKind.COMPARISON)
    second = service.create_report(env.collection_id, ReportKind.COMPARISON)
    forced = service.create_report(env.collection_id, ReportKind.COMPARISON, force=True)

    assert second.disposition == "reused"
    assert second.record.id == first.record.id
    assert forced.disposition == "generated"
    assert forced.record.id != first.record.id
    assert len(provider.prompts) == 2


def test_report_goes_stale_when_sources_change(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env)
    service = _service(env, provider)
    result = service.create_report(env.collection_id, ReportKind.REVIEW)
    _rewrite_summary(env, "paper-a")

    view = service.get_report(result.record.id)

    assert view.source_status.stale is True
    assert "summary_content_changed" in view.source_status.reasons
    assert view.report is not None
    listed = service.list_reports(env.collection_id)
    assert [item.record.id for item in listed] == [result.record.id]
    assert listed[0].source_status.stale is True
    # A stale report is not reused: the next submission regenerates.
    regenerated = service.create_report(env.collection_id, ReportKind.REVIEW)
    assert regenerated.disposition == "generated"


def test_report_partial_coverage_requires_explicit_opt_in(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    _drop_summary(env, "paper-c")
    provider = _report_provider(env)
    service = _service(env, provider)

    with pytest.raises(ScopeError):
        service.create_report(env.collection_id, ReportKind.REVIEW)

    result = service.create_report(env.collection_id, ReportKind.REVIEW, allow_partial=True)

    assert result.report.coverage.partial is True
    assert result.report.coverage.missing_summary_paper_ids == ["paper-c"]
    assert result.report.coverage.included_paper_ids == ["paper-a", "paper-b"]


def test_report_citation_failure_is_repaired(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env, bad_calls=1)
    service = _service(env, provider)

    result = service.create_report(env.collection_id, ReportKind.REVIEW)

    assert result.record.status == "completed"
    with connect_database(env.database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute(
                "SELECT stage FROM generation_llm_calls WHERE generation_run_id = ?",
                (result.record.run_id,),
            )
        ]
    assert stages == ["answer", "repair"]


def test_report_retries_when_first_repair_is_still_invalid(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env, bad_calls=2)
    service = _service(env, provider)

    result = service.create_report(env.collection_id, ReportKind.REVIEW)

    assert result.record.status == "completed"
    with connect_database(env.database_path) as connection:
        stages = [
            row[0]
            for row in connection.execute(
                "SELECT stage FROM generation_llm_calls WHERE generation_run_id = ?",
                (result.record.run_id,),
            )
        ]
    assert stages == ["answer", "repair", "repair"]
    assert "referenced but missing from the citations array" in provider.prompts[-1]
    assert "Do not return the candidate unchanged" in provider.prompts[-1]


def test_report_fails_atomically_after_bounded_repair(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env, bad_calls=3)
    service = _service(env, provider)

    with pytest.raises(AnswerValidationError):
        service.create_report(env.collection_id, ReportKind.GAPS)

    records = service.list_reports(env.collection_id)
    assert len(records) == 1
    record = records[0].record
    assert record.status == "failed"
    assert record.error is not None and record.error.startswith("invalid_answer")
    with connect_database(env.database_path) as connection:
        run_status = connection.execute(
            "SELECT status FROM generation_runs WHERE id = ?", (record.run_id,)
        ).fetchone()[0]
        artifacts = connection.execute(
            "SELECT COUNT(*) FROM collection_artifacts WHERE generation_run_id = ?",
            (record.run_id,),
        ).fetchone()[0]
    assert run_status == "failed"
    assert artifacts == 0


def test_report_reuses_matching_synthesis(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    _run_synthesis(env)
    provider = _report_provider(env)
    service = _service(env, provider)

    result = service.create_report(env.collection_id, ReportKind.REVIEW)

    assert result.report.synthesis_artifact_id is not None
    assert "collection_synthesis" in provider.prompts[0]


def test_queued_report_rejects_changed_sources(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env)
    service = _service(env, provider)
    submission = service.submit_report(env.collection_id, ReportKind.REVIEW)
    assert submission.run_id is not None
    _rewrite_summary(env, "paper-a")

    with pytest.raises(StaleSourceError):
        service.execute_report(submission.run_id)

    record = service.list_reports(env.collection_id)[0].record
    assert record.status == "failed"
    assert record.error is not None and record.error.startswith("stale_source")


def test_dispatcher_executes_answer_synthesis_and_report(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    settings = LlmSettings()

    qa_provider = FakeProvider(collection_responder(env))
    qa_dispatcher = GenerationRunDispatcher(
        env.database_path, env.data_dir, settings, provider=qa_provider
    )
    conversation = qa_dispatcher.conversations.create_conversation(collection_id=env.collection_id)
    turn_submission = qa_dispatcher.conversations.submit_turn(
        conversation.id, "这些论文有什么共同主题？"
    )
    turn = qa_dispatcher.execute_run(turn_submission.run_id)
    assert isinstance(turn, AssistantTurn)
    assert turn.qa_record.id

    synthesis_provider = _report_provider(env)  # unused; synthesis needs its own shape
    synthesis_dispatcher = GenerationRunDispatcher(
        env.database_path, env.data_dir, settings, provider=synthesis_provider
    )
    synthesis_submission = synthesis_dispatcher.syntheses.submit_synthesis(env.collection_id)
    assert synthesis_submission.run_id is not None
    with pytest.raises(AnswerValidationError):
        # The report-shaped payload is not a valid synthesis, even after repair.
        synthesis_dispatcher.execute_run(synthesis_submission.run_id)

    report_provider = _report_provider(env)
    report_dispatcher = GenerationRunDispatcher(
        env.database_path, env.data_dir, settings, provider=report_provider
    )
    report_submission = report_dispatcher.reports.submit_report(env.collection_id, ReportKind.GAPS)
    assert report_submission.run_id is not None
    result = report_dispatcher.execute_run(report_submission.run_id)
    assert isinstance(result, CollectionReportResult)
    assert result.record.status == "completed"
    assert report_dispatcher.get_generation_run(report_submission.run_id).status == "completed"


def test_startup_interruption_leaves_no_running_products(tmp_path: Path) -> None:
    env = collection_env(tmp_path)
    provider = _report_provider(env)
    dispatcher = GenerationRunDispatcher(
        env.database_path, env.data_dir, LlmSettings(), provider=provider
    )
    conversation = dispatcher.conversations.create_conversation(collection_id=env.collection_id)
    turn_submission = dispatcher.conversations.submit_turn(conversation.id, "问题？")
    report_submission = dispatcher.reports.submit_report(env.collection_id, ReportKind.REVIEW)
    synthesis_submission = dispatcher.syntheses.submit_synthesis(env.collection_id)

    interrupted = dispatcher.interrupt_active_runs()

    assert interrupted == 3
    for run_id in (
        turn_submission.run_id,
        report_submission.run_id,
        synthesis_submission.run_id,
    ):
        assert dispatcher.get_generation_run(run_id or "").status == "interrupted"
    detail = dispatcher.conversations.get_conversation(conversation.id)
    assert detail.messages[-1].status.value == "failed"
    report = dispatcher.reports.list_reports(env.collection_id)[0].record
    assert report.status == "failed"
    assert report.error is not None and report.error.startswith("interrupted")


def _run_synthesis(env: CollectionEnv) -> None:
    def respond(prompt: str) -> object:
        citations = [
            {
                "citation_id": f"c-{index}",
                "paper_id": paper_id,
                "artifact_kind": "summary_json",
                "artifact_id": env.artifact_ids[paper_id]["summary_json"],
                "artifact_sha256": env.artifact_shas[paper_id]["summary_json"],
                "summary_path": "identity.title",
            }
            for index, paper_id in enumerate(env.paper_ids)
        ]
        return json.dumps(
            {
                "schema_version": "1",
                "overview": "The collection studies systems.",
                "themes": [
                    {
                        "name": "Systems",
                        "description": "All papers study systems.",
                        "paper_ids": list(env.paper_ids),
                        "citation_ids": [citation["citation_id"] for citation in citations],
                    }
                ],
                "comparison_matrix": {"dimensions": [], "rows": []},
                "claims": [
                    {
                        "text": "The collection studies systems.",
                        "citation_ids": [citation["citation_id"] for citation in citations],
                    }
                ],
                "citations": citations,
                "coverage": {
                    "included_paper_ids": list(env.paper_ids),
                    "missing_summary_paper_ids": [],
                    "partial": False,
                },
            }
        )

    CollectionSynthesisService(
        env.database_path, env.data_dir, LlmSettings(), provider=FakeProvider(respond)
    ).synthesize(env.collection_id)
