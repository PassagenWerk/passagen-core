import httpx
import pytest

from passagen.config import ArxivSettings, CrossrefSettings, ProvidersSettings
from passagen.providers import ProviderUnavailableError, check_provider_health


def test_provider_health_records_results_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PASSAGEN_API_KEY", "test-key")

    def get(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/api/isalive"):
            return httpx.Response(200, text="true")
        if url.endswith("/models"):
            return httpx.Response(503)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", get)

    health = check_provider_health(ProvidersSettings())

    assert health.statuses["crossref"].available is True
    assert health.statuses["arxiv"].available is True
    assert health.statuses["grobid"].available is True
    assert health.statuses["llm"].available is False
    with pytest.raises(ProviderUnavailableError, match="HTTP 503"):
        health.require("llm")


def test_provider_health_does_not_probe_disabled_metadata_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        requested.append(url)
        return httpx.Response(200, text="true")

    monkeypatch.setattr(httpx, "get", get)
    settings = ProvidersSettings(
        crossref=CrossrefSettings(enabled=False),
        arxiv=ArxivSettings(enabled=False),
    )

    health = check_provider_health(settings)

    assert health.statuses["crossref"].detail == "disabled by configuration"
    assert health.statuses["arxiv"].detail == "disabled by configuration"
    assert not any("crossref" in url or "arxiv" in url for url in requested)
