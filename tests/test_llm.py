import json

import httpx
import pytest

from passagen.config import LlmSettings
from passagen.external import LlmProviderError, OpenAICompatibleProvider


def test_openai_compatible_provider_sends_json_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASSAGEN_API_KEY", "test-key")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 1000
        assert payload["messages"] == [{"role": "user", "content": "summarize"}]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = OpenAICompatibleProvider(LlmSettings(base_url="https://llm.test/v1"), client=client)

    response = provider.generate("summarize", max_tokens=1000)

    assert response.content == "{}"
    assert response.input_tokens == 4
    assert response.output_tokens == 2


def test_openai_compatible_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PASSAGEN_API_KEY", raising=False)

    with pytest.raises(LlmProviderError, match="PASSAGEN_API_KEY"):
        OpenAICompatibleProvider(LlmSettings())


def test_deepseek_provider_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASSAGEN_API_KEY", "test-key")

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = OpenAICompatibleProvider(
        LlmSettings(base_url="https://api.deepseek.com"), client=client
    )

    response = provider.generate("summarize", max_tokens=1000)

    assert response.reasoning_tokens == 0
    assert response.finish_reason == "stop"


def test_configured_provider_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASSAGEN_API_KEY", "test-key")

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = OpenAICompatibleProvider(
        LlmSettings(base_url="https://litellm.test/v1", disable_thinking=True), client=client
    )

    assert provider.generate("summarize", max_tokens=1000).content == "{}"


def test_empty_response_reports_completion_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASSAGEN_API_KEY", "test-key")

    response = httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {
                "completion_tokens": 1000,
                "completion_tokens_details": {"reasoning_tokens": 1000},
            },
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: response))
    provider = OpenAICompatibleProvider(LlmSettings(), client=client)

    with pytest.raises(
        LlmProviderError,
        match=r"finish_reason=length, output_tokens=1000, reasoning_tokens=1000",
    ):
        provider.generate("summarize", max_tokens=1000)
