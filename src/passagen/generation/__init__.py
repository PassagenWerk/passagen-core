"""Generic queued generation run dispatch for background workers.

Adapters (Web runner, CLI sync entry points) claim queued runs and execute
them by kind without knowing which service implements each product. All run
state lives in the database, so a restart never leaves a permanent running
product: ``interrupt_active_runs`` marks leftover runs, pending answer
messages, and queued/running reports as interrupted/failed.
"""

from __future__ import annotations

from pathlib import Path

from passagen.assistant import repository as run_repository
from passagen.assistant.errors import AssistantNotFoundError, ScopeError
from passagen.assistant.models import AssistantTurn
from passagen.assistant.schemas import GenerationRunKind
from passagen.assistant.service import ConversationService
from passagen.config import AssistantSettings, LlmSettings
from passagen.external.llm import LlmProvider
from passagen.research import repository as research_repository
from passagen.research.reports import CollectionReportService
from passagen.research.schemas import CollectionReportResult, CollectionSynthesisResult
from passagen.research.synthesis import CollectionSynthesisService

__all__ = ["GenerationRunDispatcher"]


class GenerationRunDispatcher:
    """Claim and execute answer, synthesis, and report runs consistently."""

    def __init__(
        self,
        database_path: Path,
        data_dir: Path,
        settings: LlmSettings,
        assistant_settings: AssistantSettings | None = None,
        *,
        provider: LlmProvider | None = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.conversations = ConversationService(
            self.database_path,
            data_dir,
            settings,
            assistant_settings,
            provider=provider,
        )
        self.syntheses = CollectionSynthesisService(
            self.database_path,
            data_dir,
            settings,
            assistant_settings,
            provider=provider,
        )
        self.reports = CollectionReportService(
            self.database_path,
            data_dir,
            settings,
            assistant_settings,
            provider=provider,
        )

    def claim_next_queued_run(self) -> run_repository.GenerationRunRecord | None:
        return run_repository.claim_next_queued_run(self.database_path)

    def get_generation_run(self, run_id: str) -> run_repository.GenerationRunRecord:
        run = run_repository.get_generation_run(self.database_path, run_id)
        if run is None:
            raise AssistantNotFoundError(f"Generation run not found: {run_id}")
        return run

    def execute_run(
        self, run_id: str
    ) -> AssistantTurn | CollectionSynthesisResult | CollectionReportResult:
        """Execute a queued or claimed run, dispatching on its persisted kind."""

        run = self.get_generation_run(run_id)
        if run.kind == GenerationRunKind.ANSWER.value:
            return self.conversations.execute_turn(run_id)
        if run.kind == GenerationRunKind.COLLECTION_SYNTHESIS.value:
            return self.syntheses.execute_synthesis_run(run_id)
        if run.kind == GenerationRunKind.REPORT.value:
            return self.reports.execute_report(run_id)
        raise ScopeError(f"Unsupported generation run kind: {run.kind}")

    def interrupt_active_runs(self) -> int:
        """Mark leftover active runs and their products as interrupted/failed."""

        research_repository.interrupt_active_reports(self.database_path)
        return run_repository.interrupt_active_generation_runs(self.database_path)
