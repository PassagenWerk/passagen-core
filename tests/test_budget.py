from passagen.config import LlmProfileSettings
from passagen.providers import TokenBudget


def test_estimate_tokens_uses_character_coefficient() -> None:
    budget = TokenBudget(
        context_window_tokens=128_000,
        chars_per_token=4.0,
    )

    assert budget.estimate_tokens("a" * 400) == 100
    assert budget.estimate_tokens("a" * 401) == 101


def test_usable_input_uses_configured_context_window() -> None:
    budget = TokenBudget(
        context_window_tokens=100_000,
        chars_per_token=4.0,
    )

    assert budget.usable_input_tokens == 100_000
    assert budget.available_input_tokens(10_000) == 90_000


def test_fits_checks_full_prompt_against_reserved_output() -> None:
    budget = TokenBudget(
        context_window_tokens=1_000,
        chars_per_token=1.0,
    )

    assert budget.fits("a" * 900, max_output_tokens=100) is True
    assert budget.fits("a" * 901, max_output_tokens=100) is False


def test_from_settings_uses_profile_context_window() -> None:
    budget = TokenBudget.from_settings(LlmProfileSettings(max_context_window=64_000))

    assert budget.usable_input_tokens == 64_000
    assert budget.estimate_tokens("a" * 100) == 25
