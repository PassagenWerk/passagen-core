"""Character-coefficient token estimation and request budget checks.

Deliberately simple: tokens are estimated as ``len(text) / chars_per_token`` and the
configured safety margin absorbs the estimation error. No tokenizer dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from passagen.config import LlmSettings


@dataclass(frozen=True, slots=True)
class TokenBudget:
    context_window_tokens: int
    max_context_utilization: float
    safety_margin_tokens: int
    chars_per_token: float

    @classmethod
    def from_settings(cls, settings: LlmSettings) -> TokenBudget:
        return cls(
            context_window_tokens=settings.context_window_tokens,
            max_context_utilization=settings.max_context_utilization,
            safety_margin_tokens=settings.safety_margin_tokens,
            chars_per_token=settings.chars_per_token,
        )

    @property
    def usable_input_tokens(self) -> int:
        """Input tokens available after utilization cap and safety margin."""
        window = math.floor(self.context_window_tokens * self.max_context_utilization)
        return max(0, window - self.safety_margin_tokens)

    def estimate_tokens(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token)

    def available_input_tokens(self, max_output_tokens: int) -> int:
        """Input tokens available for a request that reserves ``max_output_tokens``."""
        return max(0, self.usable_input_tokens - max_output_tokens)

    def fits(self, prompt: str, max_output_tokens: int) -> bool:
        return self.estimate_tokens(prompt) <= self.available_input_tokens(max_output_tokens)
