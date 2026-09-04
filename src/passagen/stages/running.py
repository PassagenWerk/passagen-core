from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from passagen.config import PipelineSettings, ProvidersSettings
from passagen.providers import LlmCallStats, LlmProvider, ProviderHealthSnapshot
from passagen.stages.progress import ProgressCallback
from passagen.stages.scanning import ScanResult, scan_directory
from passagen.stages.updating import UpdateResult, update_papers


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    scan: ScanResult
    update: UpdateResult


def run_pipeline(
    directory: Path,
    *,
    database_path: Path,
    data_dir: Path,
    providers: ProvidersSettings,
    pipeline: PipelineSettings,
    recursive: bool = True,
    provider_health: ProviderHealthSnapshot | None = None,
    execution_log_dir: Path | None = None,
    summary_provider: LlmProvider | None = None,
    outline_provider: LlmProvider | None = None,
    progress: ProgressCallback | None = None,
    llm_stats: LlmCallStats | None = None,
) -> PipelineRunResult:
    scan = scan_directory(
        directory,
        data_dir=data_dir,
        database_path=database_path,
        recursive=recursive,
        progress=progress,
    )
    update = update_papers(
        database_path,
        data_dir,
        providers,
        pipeline,
        summary_provider=summary_provider,
        outline_provider=outline_provider,
        provider_health=provider_health,
        execution_log_dir=execution_log_dir,
        progress=progress,
        llm_stats=llm_stats,
    )
    return PipelineRunResult(scan, update)
