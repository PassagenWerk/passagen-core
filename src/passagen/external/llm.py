from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from passagen.config import LlmFlavor, LlmProfileSettings, LlmReasoning


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

    def __init__(self, settings: LlmProfileSettings, *, client: httpx.Client | None = None) -> None:
        self.base_url = settings.base_url.rstrip("/")
        self.model = settings.model
        self.timeout_seconds = settings.timeout_seconds
        self.flavor = settings.flavor
        self.reasoning = settings.reasoning
        self.api_key = os.environ.get(settings.api_key_env)
        self.client = client
        if not self.api_key:
            raise LlmProviderError(
                f"LLM API key is not set; configure environment variable {settings.api_key_env}"
            )

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        if self.flavor is LlmFlavor.DEEPSEEK:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
                "thinking": {
                    "type": "enabled" if self.reasoning is LlmReasoning.ENABLE else "disabled"
                },
            }
            endpoint = "chat/completions"
        else:
            payload = {
                "model": self.model,
                "input": prompt,
                "max_output_tokens": max_tokens,
                "text": {"format": {"type": "json_object"}},
                "reasoning": {
                    "effort": "medium" if self.reasoning is LlmReasoning.ENABLE else "none"
                },
            }
            endpoint = "responses"
        try:
            response = self._post(endpoint, payload)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            if self.flavor is LlmFlavor.DEEPSEEK:
                content = body["choices"][0]["message"]["content"]
                finish_reason = _string(body["choices"][0].get("finish_reason"))
            else:
                content = _responses_content(body)
                finish_reason = _responses_finish_reason(body)
        except httpx.HTTPStatusError as exc:
            response_body = exc.response.text.strip()
            detail = f"; response body: {response_body[:2000]}" if response_body else ""
            raise LlmProviderError(f"OpenAI-compatible LLM request failed: {exc}{detail}") from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmProviderError(f"OpenAI-compatible LLM request failed: {exc}") from exc
        usage = body.get("usage")
        input_tokens = _token_count(usage, "prompt_tokens")
        output_tokens = _token_count(usage, "completion_tokens")
        if self.flavor is LlmFlavor.OPENAI:
            input_tokens = _token_count(usage, "input_tokens")
            output_tokens = _token_count(usage, "output_tokens")
        reasoning_tokens = _reasoning_tokens(usage)
        if content is None and finish_reason == "length":
            content = ""
        if not isinstance(content, str) or (not content.strip() and finish_reason != "length"):
            raise LlmProviderError(
                "OpenAI-compatible LLM returned an empty response "
                f"(finish_reason={finish_reason}, output_tokens={output_tokens}, "
                f"reasoning_tokens={reasoning_tokens})"
            )
        return LlmResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            finish_reason=finish_reason,
        )

    def _post(self, endpoint: str, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.client is not None:
            return self.client.post(f"{self.base_url}/{endpoint}", json=payload, headers=headers)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            return client.post(f"{self.base_url}/{endpoint}", json=payload, headers=headers)


def _token_count(usage: object, field: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(field)
    return value if isinstance(value, int) else None


def _reasoning_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    return _token_count(details, "reasoning_tokens")


def _responses_content(body: dict[str, Any]) -> str | None:
    output_text = body.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = body.get("output")
    if not isinstance(output, list):
        return None
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        for part in item["content"]:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "".join(texts) or None


def _responses_finish_reason(body: dict[str, Any]) -> str | None:
    incomplete = body.get("incomplete_details")
    if isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
        return "length"
    if body.get("status") == "completed":
        return "stop"
    return _string(body.get("status"))


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
