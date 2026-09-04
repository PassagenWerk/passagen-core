from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from passagen.external.llm import (
    LlmProvider,
    LlmProviderError,
    LlmResponse,
)
from passagen.external.llm import (
    OpenAICompatibleProvider as ExternalOpenAICompatibleProvider,
)

OpenAICompatibleProvider = ExternalOpenAICompatibleProvider


class LlmStage(StrEnum):
    EVIDENCE = "evidence"
    SUMMARY = "summary"
    OUTLINE = "outline"


@dataclass(slots=True)
class TokenUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, response: LlmResponse | None) -> None:
        self.calls += 1
        if response is not None:
            self.input_tokens += response.input_tokens or 0
            self.output_tokens += response.output_tokens or 0


@dataclass(slots=True)
class LlmCallStats:
    by_stage: dict[LlmStage, TokenUsage] = field(
        default_factory=lambda: {stage: TokenUsage() for stage in LlmStage}
    )

    @property
    def total(self) -> TokenUsage:
        result = TokenUsage()
        for usage in self.by_stage.values():
            result.calls += usage.calls
            result.input_tokens += usage.input_tokens
            result.output_tokens += usage.output_tokens
        return result

    def record(self, stage: LlmStage, response: LlmResponse | None) -> None:
        self.by_stage[stage].add(response)


class TrackedLlmProvider:
    """Wrap an LLM provider so external-call accounting stays outside pipeline stages."""

    def __init__(self, provider: LlmProvider, stats: LlmCallStats | None = None) -> None:
        self.provider = provider
        self.stats = stats or LlmCallStats()

    @property
    def provider_name(self) -> str:
        return self.provider.provider_name

    @property
    def model(self) -> str:
        return self.provider.model

    def generate(self, stage: LlmStage, prompt: str, *, max_tokens: int) -> LlmResponse:
        try:
            response = self.provider.generate(prompt, max_tokens=max_tokens)
        except LlmProviderError:
            self.stats.record(stage, None)
            raise
        self.stats.record(stage, response)
        return response


def retry_truncated_response(
    request: Callable[[int, int], LlmResponse],
    *,
    initial_max_tokens: int,
    is_valid: Callable[[str], bool],
    max_attempts: int = 3,
    on_retry: Callable[[int], None] | None = None,
) -> LlmResponse:
    """Retry invalid length-truncated responses with a bounded token increase."""

    attempt_tokens = initial_max_tokens
    response: LlmResponse | None = None
    for attempt in range(1, max_attempts + 1):
        response = request(attempt, attempt_tokens)
        if (
            response.finish_reason != "length"
            or is_valid(response.content)
            or attempt == max_attempts
        ):
            return response
        attempt_tokens = min(attempt_tokens * 2, initial_max_tokens * 4)
        if on_retry is not None:
            on_retry(attempt_tokens)
    if response is None:
        raise AssertionError("at least one LLM attempt is required")
    return response
