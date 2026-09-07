"""Stable assistant error types.

Adapters map ``code`` to exit codes or HTTP error payloads; the code strings
are part of the public contract and must not change without a version bump.
"""

from __future__ import annotations


class AssistantError(RuntimeError):
    code = "assistant_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ScopeError(AssistantError):
    """A conversation, plan, or citation references resources outside its scope."""

    code = "invalid_scope"


class ContextPlanError(AssistantError):
    """The planner produced a plan that violates scope, artifact, or budget rules."""

    code = "invalid_context_plan"


class AnswerValidationError(AssistantError):
    """A generated answer failed schema or citation validation after bounded repair."""

    code = "invalid_answer"


class CitationValidationError(AssistantError):
    """A citation does not resolve to the source snapshot it claims to use."""

    code = "invalid_citation"


class InsufficientEvidenceError(AssistantError):
    """The available sources cannot support a grounded answer to the question."""

    code = "insufficient_evidence"


class StaleSourceError(AssistantError):
    """A reused answer or report was produced from a different source fingerprint."""

    code = "stale_source"


class ProviderCallError(AssistantError):
    """The LLM provider failed or returned unusable content for a generation call."""

    code = "provider_error"
