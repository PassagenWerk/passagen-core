"""Adapter-independent processing service shared by the CLI and the Web runner."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from passagen.config import Settings
from passagen.processing import store
from passagen.processing.models import (
    ProcessingError,
    ProcessingRun,
    ProgressEvent,
    RunConflictError,
    RunMode,
    RunNotFoundError,
    RunNotQueuedError,
    RunPaperFailure,
    RunResultSummary,
    RunStatus,
    UnknownPaperError,
)
from passagen.prompting import (
    PromptTemplateError,
    load_abstract_fix_prompt_template,
    load_outline_prompt_template,
    load_summary_prompt_templates,
)
from passagen.providers import (
    LlmCallStats,
    LlmProvider,
    ProviderHealthSnapshot,
)
from passagen.stages.progress import ProgressCallback
from passagen.stages.updating import REBUILD_STAGES, UpdateEvent, UpdateResult, update_papers
from passagen.storage.repository import get_paper

logger = logging.getLogger(__name__)


class ProcessingService:
    """Create, persist, and execute batch update runs for a library.

    The same service backs ``passagen update`` (synchronous execution) and the
    Web background runner (queued execution), so runs, LLM calls, and
    diagnostics are identical regardless of the initiating adapter.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider_health: ProviderHealthSnapshot | None = None,
    ) -> None:
        self._settings = settings
        self._provider_health = provider_health
        self._create_lock = threading.Lock()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def database_path(self) -> Path:
        return self._settings.resolved_database_path

    @property
    def data_dir(self) -> Path:
        return self._settings.resolved_data_dir

    def start_update(
        self,
        paper_ids: list[str],
        *,
        mode: RunMode = "continue",
        from_stage: str | None = None,
    ) -> ProcessingRun:
        if from_stage is not None and mode != "rebuild":
            raise ProcessingError("from_stage requires mode='rebuild'")
        if from_stage is not None and from_stage not in REBUILD_STAGES:
            raise ProcessingError(
                f"Unknown stage: {from_stage}; expected one of {', '.join(REBUILD_STAGES)}"
            )
        selected = list(dict.fromkeys(paper_ids))
        missing = [pid for pid in selected if get_paper(self.database_path, pid) is None]
        if missing:
            raise UnknownPaperError(missing)
        with self._create_lock:
            conflict = store.find_active_run_for_papers(self.database_path, selected)
            if conflict is not None:
                overlapping = sorted(set(selected) & set(conflict.paper_ids))
                raise RunConflictError(
                    f"Run {conflict.id} is already {conflict.status.value} for: "
                    f"{', '.join(overlapping)}"
                )
            run = store.create_run(
                self.database_path, paper_ids=selected, mode=mode, from_stage=from_stage
            )
        self._write_snapshot(run)
        self._recorder(run).emit(UpdateEvent(None, "update", 0, len(selected), "Run queued."))
        logger.info(
            "processing run created: run_id=%s mode=%s from_stage=%s papers=%s",
            run.id,
            run.mode,
            run.from_stage,
            len(selected),
        )
        return run

    def get_run(self, run_id: str) -> ProcessingRun:
        run = store.get_run(self.database_path, run_id)
        if run is None:
            raise RunNotFoundError(f"Processing run not found: {run_id}")
        return run

    def list_runs(
        self,
        *,
        status: str | None = None,
        paper_id: str | None = None,
        limit: int = 50,
    ) -> list[ProcessingRun]:
        run_status = RunStatus(status) if status is not None else None
        return store.list_runs(
            self.database_path, status=run_status, paper_id=paper_id, limit=limit
        )

    def list_events(self, run_id: str, *, after: int = 0) -> list[ProgressEvent]:
        self.get_run(run_id)
        events_path = self._run_dir(run_id) / "events.jsonl"
        if not events_path.is_file():
            return []
        events: list[ProgressEvent] = []
        with events_path.open(encoding="utf-8") as events_file:
            for line in events_file:
                line = line.strip()
                if not line:
                    continue
                event = ProgressEvent.model_validate_json(line)
                if event.sequence > after:
                    events.append(event)
        return events

    def next_queued_run(self) -> ProcessingRun | None:
        return store.next_queued_run(self.database_path)

    def interrupt_active_runs(self) -> list[ProcessingRun]:
        interrupted = store.interrupt_active_runs(self.database_path)
        for run in interrupted:
            logger.warning(
                "processing run interrupted by restart: run_id=%s status=%s",
                run.id,
                run.status.value,
            )
        return interrupted

    def execute_run(
        self,
        run_id: str,
        *,
        progress: ProgressCallback | None = None,
        execution_log_dir: Path | None = None,
        summary_provider: LlmProvider | None = None,
        outline_provider: LlmProvider | None = None,
        abstract_provider: LlmProvider | None = None,
        provider_health: ProviderHealthSnapshot | None = None,
        llm_stats: LlmCallStats | None = None,
    ) -> UpdateResult:
        run = store.get_run(self.database_path, run_id)
        if run is None:
            raise RunNotFoundError(f"Processing run not found: {run_id}")
        if not store.claim_run(self.database_path, run_id):
            raise RunNotQueuedError(f"Processing run {run_id} is {run.status.value}, not queued")
        recorder = self._recorder(run)
        logger.info("processing run started: run_id=%s papers=%s", run.id, len(run.paper_ids))

        def on_event(event: UpdateEvent) -> None:
            recorder.emit(event)
            store.update_run_progress(
                self.database_path,
                run_id,
                current_paper_id=event.paper_id,
                current_stage=event.stage,
            )

        health = provider_health or self._provider_health
        try:
            result = update_papers(
                self.database_path,
                self.data_dir,
                self._settings.providers,
                self._settings.pipeline,
                paper_ids=run.paper_ids,
                from_stage=run.from_stage,
                summary_provider=summary_provider,
                outline_provider=outline_provider,
                abstract_provider=abstract_provider,
                provider_health=health,
                execution_log_dir=execution_log_dir or self._run_dir(run_id),
                progress=progress,
                on_event=on_event,
                llm_stats=llm_stats,
            )
        except KeyboardInterrupt:
            store.finish_run(
                self.database_path,
                run_id,
                status=RunStatus.INTERRUPTED,
                error="Interrupted while running",
            )
            raise
        except Exception as exc:
            logger.error("processing run failed: run_id=%s error=%s", run_id, exc)
            recorder.emit(UpdateEvent(None, "failed", 0, len(run.paper_ids), f"Run failed: {exc}"))
            store.finish_run(self.database_path, run_id, status=RunStatus.FAILED, error=str(exc))
            raise
        summary = RunResultSummary(
            updated=[paper.id for paper in result.updated],
            skipped=[paper.id for paper in result.skipped],
            failed=[
                RunPaperFailure(
                    paper_id=failure.paper_id,
                    category=failure.category or "processing",
                    message=failure.message,
                )
                for failure in result.failures
            ],
            warnings=[
                RunPaperFailure(
                    paper_id=warning.paper_id, category="warning", message=warning.message
                )
                for warning in result.warnings
            ],
        )
        store.finish_run(self.database_path, run_id, status=RunStatus.COMPLETED, result=summary)
        logger.info(
            "processing run finished: run_id=%s updated=%s skipped=%s failed=%s",
            run_id,
            len(summary.updated),
            len(summary.skipped),
            len(summary.failed),
        )
        return result

    def _run_dir(self, run_id: str) -> Path:
        return self.data_dir / "runs" / run_id

    def _recorder(self, run: ProcessingRun) -> _EventRecorder:
        return _EventRecorder(self._run_dir(run.id), run.id)

    def _write_snapshot(self, run: ProcessingRun) -> None:
        snapshot = {
            "run_id": run.id,
            "mode": run.mode,
            "from_stage": run.from_stage,
            "paper_ids": run.paper_ids,
            "created_at": run.created_at.isoformat(),
            "config": {
                "providers": self._settings.providers.model_dump(mode="json"),
                "pipeline": self._settings.pipeline.model_dump(mode="json"),
                "prompts": _prompt_versions(self._settings),
            },
        }
        run_dir = self._run_dir(run.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


class _EventRecorder:
    """Append structured progress events to ``runs/<run-id>/events.jsonl``."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self._run_id = run_id
        self._path = run_dir / "events.jsonl"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        if self._path.is_file():
            with self._path.open(encoding="utf-8") as events_file:
                self._sequence = sum(1 for line in events_file if line.strip())

    def emit(self, event: UpdateEvent) -> None:
        self._sequence += 1
        progress_event = ProgressEvent(
            sequence=self._sequence,
            run_id=self._run_id,
            paper_id=event.paper_id,
            stage=event.stage,
            current=event.current,
            total=event.total,
            message=event.message,
        )
        with self._path.open("a", encoding="utf-8") as events_file:
            events_file.write(progress_event.model_dump_json() + "\n")


def _prompt_versions(settings: Settings) -> dict[str, str]:
    summarization = settings.pipeline.summarization
    versions: dict[str, str] = {}
    try:
        templates = load_summary_prompt_templates(
            summarization.facts_prompt_path,
            summarization.summary_prompt_path,
            summarization.repair_prompt_path,
            summarization.full_prompt_path,
            summarization.reduce_prompt_path,
        )
        versions.update(
            {
                "evidence": templates.evidence.sha256,
                "summary": templates.summary.sha256,
                "full": templates.full.sha256,
                "reduce": templates.reduce.sha256,
                "repair": templates.repair.sha256,
            }
        )
    except PromptTemplateError as exc:
        versions["summarization"] = f"unavailable: {exc}"
    try:
        abstract_fix = load_abstract_fix_prompt_template(
            settings.pipeline.abstract_fixing.prompt_path
        )
        versions["abstract_fix"] = abstract_fix.sha256
    except PromptTemplateError as exc:
        versions["abstract_fix"] = f"unavailable: {exc}"
    try:
        outline = load_outline_prompt_template(settings.pipeline.outlining.prompt_path)
        versions["outline"] = outline.sha256
    except PromptTemplateError as exc:
        versions["outline"] = f"unavailable: {exc}"
    return versions
