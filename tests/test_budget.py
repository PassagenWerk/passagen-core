from passagen.config import LlmSettings
from passagen.providers import TokenBudget


def test_estimate_tokens_uses_character_coefficient() -> None:
    budget = TokenBudget(
        context_window_tokens=128_000,
        max_context_utilization=0.65,
        safety_margin_tokens=8_000,
        chars_per_token=4.0,
    )

    assert budget.estimate_tokens("a" * 400) == 100
    assert budget.estimate_tokens("a" * 401) == 101


def test_usable_input_applies_utilization_and_safety_margin() -> None:
    budget = TokenBudget(
        context_window_tokens=100_000,
        max_context_utilization=0.6,
        safety_margin_tokens=5_000,
        chars_per_token=4.0,
    )

    assert budget.usable_input_tokens == 55_000
    assert budget.available_input_tokens(10_000) == 45_000


def test_fits_checks_full_prompt_against_reserved_output() -> None:
    budget = TokenBudget(
        context_window_tokens=1_000,
        max_context_utilization=1.0,
        safety_margin_tokens=100,
        chars_per_token=1.0,
    )

    assert budget.fits("a" * 800, max_output_tokens=100) is True
    assert budget.fits("a" * 801, max_output_tokens=100) is False


def test_from_settings_uses_global_llm_parameters() -> None:
    budget = TokenBudget.from_settings(
        LlmSettings(
            context_window_tokens=64_000,
            max_context_utilization=0.5,
            safety_margin_tokens=2_000,
            chars_per_token=2.0,
        )
    )

    assert budget.usable_input_tokens == 30_000
    assert budget.estimate_tokens("a" * 100) == 50
