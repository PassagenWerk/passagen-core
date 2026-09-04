from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_FILENAME = "passagen.yaml"
DEFAULT_DATA_DIR = Path("data")
LEGACY_CONFIG_PATH = Path("passagen.yaml")
ENV_PREFIX = "PASSAGEN_"


class CrossrefSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    base_url: str = "https://api.crossref.org"
    mailto: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)


class ArxivSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    base_url: str = "https://export.arxiv.org"
    timeout_seconds: float = Field(default=10.0, gt=0)


class GrobidSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:8070"
    timeout_seconds: float = Field(default=60.0, gt=0)


class LlmSettings(BaseModel):
    """Global LLM call parameters shared by every LLM-powered stage."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "PASSAGEN_API_KEY"
    timeout_seconds: float = Field(default=120.0, gt=0)
    disable_thinking: bool = False
    context_window_tokens: int = Field(default=128_000, ge=1_000)
    max_context_utilization: float = Field(default=0.65, gt=0, le=1.0)
    safety_margin_tokens: int = Field(default=8_000, ge=0)
    chars_per_token: float = Field(default=4.0, gt=0)


class ProvidersSettings(BaseModel):
    """External provider services, grouped by provider."""

    model_config = ConfigDict(extra="forbid")

    healthcheck_timeout_seconds: float = Field(default=3.0, gt=0)
    crossref: CrossrefSettings = Field(default_factory=CrossrefSettings)
    arxiv: ArxivSettings = Field(default_factory=ArxivSettings)
    grobid: GrobidSettings = Field(default_factory=GrobidSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)


class ParserBackend(StrEnum):
    AUTO = "auto"
    GROBID = "grobid"
    PYMUPDF = "pymupdf"


class MetadataSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_pages: int = Field(default=2, ge=1, le=10)


class ParsingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parser: ParserBackend = ParserBackend.AUTO
    min_text_characters: int = Field(default=10, ge=1)


class SummarizationStrategy(StrEnum):
    AUTO = "auto"
    FULL = "full"
    HIERARCHICAL = "hierarchical"


class SummarizationSettings(BaseModel):
    """Summary strategy and chunking parameters; global LLM limits live in providers.llm."""

    model_config = ConfigDict(extra="forbid")

    strategy: SummarizationStrategy = SummarizationStrategy.AUTO
    chunk_max_input_tokens: int = Field(default=24_000, ge=1_000)
    chunk_overlap_paragraphs: int = Field(default=1, ge=0, le=5)
    fact_max_output_tokens: int = Field(default=1_500, ge=100)
    summary_max_output_tokens: int = Field(default=3_000, ge=100)
    facts_prompt_path: Path | None = None
    summary_prompt_path: Path | None = None
    full_prompt_path: Path | None = None
    reduce_prompt_path: Path | None = None
    repair_prompt_path: Path | None = None


class OutliningSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_output_tokens: int = Field(default=4_000, ge=100)
    prompt_path: Path | None = None


class PipelineSettings(BaseModel):
    """Processing stage parameters, grouped by stage."""

    model_config = ConfigDict(extra="forbid")

    metadata: MetadataSettings = Field(default_factory=MetadataSettings)
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    summarization: SummarizationSettings = Field(default_factory=SummarizationSettings)
    outlining: OutliningSettings = Field(default_factory=OutliningSettings)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
    )

    # data_dir only comes from the command line (or the ./data default); it is
    # rejected in YAML and via PASSAGEN_DATA_DIR by load_settings below.
    data_dir: Path = DEFAULT_DATA_DIR
    database_path: Path | None = None
    debug: bool = False
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def resolved_database_path(self) -> Path:
        if self.database_path:
            return self.database_path.expanduser().resolve()
        return self.resolved_data_dir / "passagen.db"


class ConfigError(ValueError):
    pass


def load_settings(
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load settings from ``<data_dir>/passagen.yaml``, environment, and CLI overrides.

    The config file lives inside the data directory so a library is self-contained.
    ``data_dir`` itself can only come from the command line (default ``./data``);
    setting it in YAML or via ``PASSAGEN_DATA_DIR`` is rejected.
    """
    if f"{ENV_PREFIX}DATA_DIR" in os.environ:
        raise ConfigError(
            f"{ENV_PREFIX}DATA_DIR is not supported; use the --data-dir command line option instead"
        )

    cli_overrides = {key: value for key, value in (overrides or {}).items() if value is not None}
    data_dir = Path(cli_overrides.get("data_dir") or DEFAULT_DATA_DIR).expanduser()

    path = _resolve_config_path(config_path, data_dir)
    values = _read_config(path) if path is not None else {}

    if "data_dir" in values:
        raise ConfigError(
            f"Config {path} must not set 'data_dir'; use the --data-dir command line option instead"
        )

    _remove_environment_overrides(values)
    values.update(cli_overrides)
    # Pass data_dir as an init keyword so the environment can never populate it.
    values["data_dir"] = data_dir

    try:
        return Settings(**values)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def _resolve_config_path(config_path: Path | None, data_dir: Path) -> Path | None:
    if config_path is not None:
        return config_path.expanduser()

    candidate = data_dir / CONFIG_FILENAME
    legacy = LEGACY_CONFIG_PATH
    if candidate.exists():
        if legacy.exists():
            raise ConfigError(
                f"Config exists in both {candidate} and {legacy}; keep only {candidate}"
            )
        return candidate
    if legacy.exists():
        raise ConfigError(f"Config location has moved to {candidate}; move {legacy} there")
    return None


def _read_config(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as config_file:
            document = yaml.safe_load(config_file)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc

    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigError(f"Config {path} must contain a mapping")

    if "passagen" not in document:
        return document

    unknown_sections = set(document) - {"passagen", "providers", "pipeline"}
    if unknown_sections:
        names = ", ".join(sorted(str(name) for name in unknown_sections))
        raise ConfigError(f"Config {path} contains unknown sections: {names}")

    passagen_values = document["passagen"]
    if not isinstance(passagen_values, dict):
        raise ConfigError(f"Config {path} section 'passagen' must contain a mapping")
    values = dict(passagen_values)
    if "providers" in document:
        values["providers"] = document["providers"]
    if "pipeline" in document:
        values["pipeline"] = document["pipeline"]
    return values


def _remove_environment_overrides(values: dict[str, Any]) -> None:
    for environment_name in os.environ:
        if not environment_name.startswith(ENV_PREFIX):
            continue
        path = environment_name.removeprefix(ENV_PREFIX).lower().split("__")
        current: dict[str, Any] = values
        for part in path[:-1]:
            nested = current.get(part)
            if not isinstance(nested, dict):
                break
            current = nested
        else:
            current.pop(path[-1], None)
