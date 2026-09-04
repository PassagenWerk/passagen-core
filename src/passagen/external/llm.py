from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from passagen.config import LlmSettings


class LlmProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LlmResponse:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None


class LlmProvider(Protocol):
    provider_name: str
    model: str

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse: ...


class OpenAICompatibleProvider:
    provider_name = "openai_compatible"

    def __init__(self, settings: LlmSettings, *, client: httpx.Client | None = None) -> None:
        self.base_url = settings.base_url.rstrip("/")
        self.model = settings.model
        self.timeout_seconds = settings.timeout_seconds
        self.disable_thinking = settings.disable_thinking
        self.api_key = os.environ.get(settings.api_key_env)
        self.client = client
        if not self.api_key:
            raise LlmProviderError(
                f"LLM API key is not set; configure environment variable {settings.api_key_env}"
            )

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
        }
        if self.disable_thinking or "deepseek.com" in self.base_url:
            payload["thinking"] = {"type": "disabled"}
        try:
            response = self._post(payload)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            content = body["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmProviderError(f"OpenAI-compatible LLM request failed: {exc}") from exc
        usage = body.get("usage")
        output_tokens = _token_count(usage, "completion_tokens")
        reasoning_tokens = _reasoning_tokens(usage)
        finish_reason = _string(body["choices"][0].get("finish_reason"))
        if not isinstance(content, str) or not content.strip():
            raise LlmProviderError(
                "OpenAI-compatible LLM returned an empty response "
                f"(finish_reason={finish_reason}, output_tokens={output_tokens}, "
                f"reasoning_tokens={reasoning_tokens})"
            )
        return LlmResponse(
            content=content,
            input_tokens=_token_count(usage, "prompt_tokens"),
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            finish_reason=finish_reason,
        )

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.client is not None:
            return self.client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
        with httpx.Client(timeout=self.timeout_seconds) as client:
            return client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)


def _token_count(usage: object, field: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(field)
    return value if isinstance(value, int) else None


def _reasoning_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    return _token_count(details, "reasoning_tokens")


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
