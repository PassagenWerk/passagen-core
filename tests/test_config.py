from pathlib import Path

import pytest

from passagen.config import (
    CONFIG_FILENAME,
    ConfigError,
    LlmPurpose,
    LlmSettings,
    Settings,
    load_settings,
)


def test_defaults_to_deepseek_flash() -> None:
    settings = LlmSettings()

    assert settings.default.base_url == "https://api.deepseek.com/v1"
    assert settings.default.model == "deepseek-flash-v4"
    assert settings.default.max_context_window == 1_000_000
    assert settings.resolve(LlmPurpose.QA_ANSWER) == ("default", settings.default)


def test_default_generation_budgets_match_default_llm() -> None:
    settings = Settings()

    assert settings.pipeline.summarization.chunk_max_input_tokens == 128_000
    assert settings.pipeline.summarization.fact_max_output_tokens == 6_000
    assert settings.pipeline.summarization.summary_max_output_tokens == 20_000
    assert settings.pipeline.outlining.max_output_tokens == 40_000
    assert settings.assistant.answer_max_output_tokens == 8_192


def test_routes_selected_tasks_to_advanced_profiles() -> None:
    settings = LlmSettings.model_validate(
        {
            "default": {"model": "fast-model"},
            "profiles": {
                "reasoning": {
                    "model": "reasoning-model",
                    "reasoning": "enable",
                }
            },
            "tasks": {"summary_synthesis": "reasoning"},
        }
    )

    profile_name, profile = settings.resolve(LlmPurpose.SUMMARY_SYNTHESIS)
    assert profile_name == "reasoning"
    assert profile.model == "reasoning-model"
    assert settings.resolve(LlmPurpose.SUMMARY_EVIDENCE) == ("default", settings.default)


def test_rejects_task_route_to_unknown_profile() -> None:
    with pytest.raises(ValueError, match="unknown profiles: missing"):
        LlmSettings.model_validate(
            {"default": {"model": "fast-model"}, "tasks": {"qa_answer": "missing"}}
        )


def test_reads_config_from_default_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / CONFIG_FILENAME).write_text("passagen:\n  debug: true\n")

    settings = load_settings()

    assert settings.debug is True
    assert settings.resolved_data_dir == data_dir
    assert settings.resolved_database_path == data_dir / "passagen.db"


def test_reads_config_from_data_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    (data_dir / CONFIG_FILENAME).write_text("passagen:\n  debug: true\n")

    settings = load_settings(None, {"data_dir": data_dir})

    assert settings.debug is True
    assert settings.resolved_data_dir == data_dir


def test_loads_yaml_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("passagen:\n  debug: false\n")
    monkeypatch.setenv("PASSAGEN_DEBUG", "true")

    settings = load_settings(config_path)

    assert settings.debug is True


def test_loads_layered_providers_pipeline_config_and_nested_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
passagen: {}
providers:
  crossref:
    enabled: false
    timeout_seconds: 20
  grobid:
    base_url: http://grobid.test:8070
    timeout_seconds: 30
  llm:
    default:
      model: test-model
pipeline:
  metadata:
    first_pages: 4
  parsing:
    parser: pymupdf
  summarization:
    strategy: hierarchical
    chunk_max_input_tokens: 8000
"""
    )
    monkeypatch.setenv("PASSAGEN_PROVIDERS__CROSSREF__TIMEOUT_SECONDS", "3.5")

    settings = load_settings(config_path)

    assert settings.pipeline.metadata.first_pages == 4
    assert settings.providers.crossref.enabled is False
    assert settings.providers.crossref.timeout_seconds == 3.5
    assert settings.providers.arxiv.enabled is True
    assert settings.providers.grobid.base_url == "http://grobid.test:8070"
    assert settings.providers.grobid.timeout_seconds == 30
    assert settings.pipeline.parsing.parser.value == "pymupdf"
    assert settings.providers.llm.default.model == "test-model"
    assert settings.pipeline.summarization.strategy.value == "hierarchical"
    assert settings.pipeline.summarization.chunk_max_input_tokens == 8000
    assert settings.pipeline.outlining.max_output_tokens == 40000


def test_data_dir_comes_from_command_line_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings(None, {"data_dir": Path("/from/cli")})

    assert settings.data_dir == Path("/from/cli")


def test_rejects_data_dir_in_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("passagen:\n  data_dir: /from/file\n")

    with pytest.raises(ConfigError, match="must not set 'data_dir'"):
        load_settings(config_path)


def test_rejects_data_dir_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PASSAGEN_DATA_DIR", "/from/environment")

    with pytest.raises(ConfigError, match="PASSAGEN_DATA_DIR"):
        load_settings()


def test_rejects_config_in_both_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / CONFIG_FILENAME).write_text("passagen: {}\n")
    (tmp_path / CONFIG_FILENAME).write_text("passagen: {}\n")

    with pytest.raises(ConfigError, match="both"):
        load_settings()


def test_rejects_legacy_config_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / CONFIG_FILENAME).write_text("passagen:\n  debug: true\n")

    with pytest.raises(ConfigError, match="has moved"):
        load_settings()


def test_rejects_unknown_config_field(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("passagen:\n  unknown: true\n")

    with pytest.raises(ConfigError):
        load_settings(config_path)


def test_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- invalid\n- config\n")

    with pytest.raises(ConfigError, match="must contain a mapping"):
        load_settings(config_path)


def test_missing_config_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.data_dir == Path("data")
    assert settings.debug is False


def test_empty_yaml_uses_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("")

    settings = load_settings(config_path)

    assert settings.data_dir == Path("data")
    assert settings.debug is False


def test_rejects_invalid_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("passagen: [\n")

    with pytest.raises(ConfigError, match="Cannot read config"):
        load_settings(config_path)


def test_resolves_relative_prompt_paths_against_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / CONFIG_FILENAME).write_text(
        "pipeline:\n"
        "  abstract_fixing:\n"
        "    prompt_path: prompts/abstract.txt\n"
        "  summarization:\n"
        "    facts_prompt_path: prompts/facts.txt\n"
        "  outlining:\n"
        "    prompt_path: /absolute/outline.txt\n"
    )

    settings = load_settings()

    assert settings.pipeline.abstract_fixing.prompt_path == Path("data/prompts/abstract.txt")
    assert settings.pipeline.summarization.facts_prompt_path == Path("data/prompts/facts.txt")
    assert settings.pipeline.outlining.prompt_path == Path("/absolute/outline.txt")


def test_loads_assistant_settings_and_resolves_prompt_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "assistant:\n"
        "  rewrite_max_output_tokens: 1500\n"
        "  answer_max_output_tokens: 12000\n"
        "  truncated_response_max_attempts: 4\n"
        "  max_history_messages: 6\n"
        "  max_raw_sections: 5\n"
        "  answer_prompt_path: prompts/answer.txt\n"
    )

    settings = load_settings(config_path)

    assert settings.assistant.rewrite_max_output_tokens == 1500
    assert settings.assistant.answer_max_output_tokens == 12000
    assert settings.assistant.truncated_response_max_attempts == 4
    assert settings.assistant.max_history_messages == 6
    assert settings.assistant.max_raw_sections == 5
    assert settings.assistant.answer_prompt_path == tmp_path / "prompts/answer.txt"
