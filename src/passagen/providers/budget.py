"""Character-coefficient token estimation and request budget checks.

Deliberately simple: tokens are estimated as ``len(text) / chars_per_token`` and the
configured safety margin absorbs the estimation error. No tokenizer dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from passagen.config import LlmProfileSettings


@dataclass(frozen=True, slots=True)
class TokenBudget:
    context_window_tokens: int
    chars_per_token: float = 4.0

    @classmethod
    def from_settings(cls, settings: LlmProfileSettings) -> TokenBudget:
        return cls(context_window_tokens=settings.max_context_window)

    @property
    def usable_input_tokens(self) -> int:
        """Input tokens available after utilization cap and safety margin."""
        return self.context_window_tokens

    def estimate_tokens(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token)

    def available_input_tokens(self, max_output_tokens: int) -> int:
        """Input tokens available for a request that reserves ``max_output_tokens``."""
        return max(0, self.usable_input_tokens - max_output_tokens)

    def fits(self, prompt: str, max_output_tokens: int) -> bool:
        return self.estimate_tokens(prompt) <= self.available_input_tokens(max_output_tokens)
