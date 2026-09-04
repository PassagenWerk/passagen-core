from pathlib import Path

import pytest

from passagen.config import DEFAULT_CONFIG_PATH, ConfigError, load_settings


def test_defaults_to_current_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / DEFAULT_CONFIG_PATH).write_text("passagen:\n  debug: true\n")

    settings = load_settings()

    assert settings.debug is True
    assert settings.resolved_data_dir == tmp_path / "data"
    assert settings.resolved_database_path == tmp_path / "data" / "passagen.db"


def test_loads_yaml_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("passagen:\n  data_dir: /from/file\n  debug: false\n")
    monkeypatch.setenv("PASSAGEN_DATA_DIR", "/from/environment")

    settings = load_settings(config_path)

    assert settings.data_dir == Path("/from/environment")
    assert settings.debug is False


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
    model: test-model
pipeline:
  metadata:
    first_pages: 4
  parsing:
    parser: pymupdf
  summarization:
    max_chunk_characters: 2000
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
    assert settings.providers.llm.model == "test-model"
    assert settings.pipeline.summarization.max_chunk_characters == 2000
    assert settings.pipeline.outlining.max_output_tokens == 4000


def test_cli_override_has_highest_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("passagen:\n  data_dir: /from/file\n")
    monkeypatch.setenv("PASSAGEN_DATA_DIR", "/from/environment")

    settings = load_settings(config_path, {"data_dir": Path("/from/cli")})

    assert settings.data_dir == Path("/from/cli")


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
