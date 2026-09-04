import pytest

from passagen.providers import (
    LlmCallStats,
    LlmProviderError,
    LlmResponse,
    LlmStage,
    TrackedLlmProvider,
)


class FakeProvider:
    provider_name = "fake"
    model = "test-model"

    def __init__(self, response: LlmResponse | None = None) -> None:
        self.response = response

    def generate(self, prompt: str, *, max_tokens: int) -> LlmResponse:
        del prompt, max_tokens
        if self.response is None:
            raise LlmProviderError("failed")
        return self.response


def test_tracked_llm_provider_aggregates_usage_by_stage() -> None:
    stats = LlmCallStats()
    provider = TrackedLlmProvider(
        FakeProvider(LlmResponse("{}", input_tokens=20, output_tokens=5)), stats
    )

    provider.generate(LlmStage.EVIDENCE, "evidence", max_tokens=100)
    provider.generate(LlmStage.EVIDENCE, "evidence", max_tokens=100)
    provider.generate(LlmStage.SUMMARY, "summary", max_tokens=100)

    assert stats.by_stage[LlmStage.EVIDENCE].calls == 2
    assert stats.by_stage[LlmStage.EVIDENCE].total_tokens == 50
    assert stats.by_stage[LlmStage.SUMMARY].input_tokens == 20
    assert stats.total.calls == 3
    assert stats.total.input_tokens == 60
    assert stats.total.output_tokens == 15
    assert stats.total.total_tokens == 75


def test_tracked_llm_provider_counts_failed_calls_without_tokens() -> None:
    stats = LlmCallStats()
    provider = TrackedLlmProvider(FakeProvider(), stats)

    with pytest.raises(LlmProviderError, match="failed"):
        provider.generate(LlmStage.OUTLINE, "outline", max_tokens=100)

    assert stats.by_stage[LlmStage.OUTLINE].calls == 1
    assert stats.total.total_tokens == 0
