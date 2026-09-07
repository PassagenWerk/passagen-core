"""Assemble delimited, budgeted source context for the answer prompt.

Each block carries a stable source header with the citation metadata the model
must copy, so generated citations can be validated against the source snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from passagen.assistant.errors import ContextPlanError
from passagen.assistant.retrieval import RetrievedSection
from passagen.assistant.schemas import (
    ArtifactRef,
    ContextPlan,
    ContextSource,
    Message,
    PaperSourceSnapshot,
)
from passagen.providers.budget import TokenBudget
from passagen.stages.summarization.schema import StructuredSummary

_HISTORY_SHARE = 0.15
_SUMMARY_SHARE = 0.45
_OUTLINE_SHARE = 0.20
_RAW_SHARE = 0.45


@dataclass(frozen=True, slots=True)
class ContextBlock:
    source: ContextSource
    header: str
    content: str

    def render(self) -> str:
        return f"{self.header}\n{self.content}"


@dataclass(frozen=True, slots=True)
class AssembledContext:
    blocks: tuple[ContextBlock, ...]

    def render(self) -> str:
        return "\n\n".join(block.render() for block in self.blocks)


def raw_section_token_cap(
    budget: TokenBudget, reserved_output_tokens: int, prompt_overhead_tokens: int
) -> int:
    available = max(
        0, budget.available_input_tokens(reserved_output_tokens) - prompt_overhead_tokens
    )
    return int(available * _RAW_SHARE)


def build_context(
    *,
    snapshot: PaperSourceSnapshot,
    plan: ContextPlan,
    history: Sequence[Message],
    summary: StructuredSummary | None,
    outline: str | None,
    sections: Sequence[RetrievedSection],
    budget: TokenBudget,
    reserved_output_tokens: int,
    prompt_overhead_tokens: int,
) -> AssembledContext:
    available = max(
        0, budget.available_input_tokens(reserved_output_tokens) - prompt_overhead_tokens
    )
    blocks: list[ContextBlock] = []
    if ContextSource.CONVERSATION in plan.sources and history:
        blocks.append(_history_block(history, budget, int(available * _HISTORY_SHARE)))
    if ContextSource.SUMMARY in plan.sources:
        if summary is None:
            raise ContextPlanError("the plan requires the summary but it was not loaded")
        blocks.append(_summary_block(snapshot, summary, budget, int(available * _SUMMARY_SHARE)))
    if ContextSource.OUTLINE in plan.sources:
        if outline is None:
            raise ContextPlanError("the plan requires the outline but it was not loaded")
        blocks.append(_outline_block(snapshot, outline, budget, int(available * _OUTLINE_SHARE)))
    if ContextSource.RAW in plan.sources:
        raw_cap = int(available * _RAW_SHARE)
        for section in sections:
            tokens = budget.estimate_tokens(section.text)
            if tokens > raw_cap:
                continue
            blocks.append(_raw_block(snapshot, section))
            raw_cap -= tokens
    if not blocks:
        raise ContextPlanError("the context plan produced no usable source blocks")
    return AssembledContext(tuple(blocks))


def _artifact_header(snapshot: PaperSourceSnapshot, kind: str) -> str:
    artifact = _artifact(snapshot, kind)
    if artifact is None:
        raise ContextPlanError(f"the source snapshot has no {kind} artifact")
    return (
        f"[source {kind} paper_id={snapshot.paper_id} artifact_id={artifact.artifact_id} "
        f"artifact_sha256={artifact.sha256}"
    )


def _artifact(snapshot: PaperSourceSnapshot, kind: str) -> ArtifactRef | None:
    for artifact in snapshot.artifacts:
        if artifact.kind == kind:
            return artifact
    return None


def _history_block(history: Sequence[Message], budget: TokenBudget, token_cap: int) -> ContextBlock:
    lines: list[str] = []
    used = 0
    for message in reversed(history):
        line = f"{message.role.value}: {message.content}"
        tokens = budget.estimate_tokens(line)
        if lines and used + tokens > token_cap:
            break
        lines.append(line)
        used += tokens
    lines.reverse()
    return ContextBlock(ContextSource.CONVERSATION, "[source conversation]", "\n".join(lines))


def _summary_block(
    snapshot: PaperSourceSnapshot,
    summary: StructuredSummary,
    budget: TokenBudget,
    token_cap: int,
) -> ContextBlock:
    content = summary.model_dump_json()
    if budget.estimate_tokens(content) > token_cap:
        raise ContextPlanError("the paper summary does not fit the context token budget")
    return ContextBlock(
        ContextSource.SUMMARY, f"{_artifact_header(snapshot, 'summary_json')}]", content
    )


def _outline_block(
    snapshot: PaperSourceSnapshot,
    outline: str,
    budget: TokenBudget,
    token_cap: int,
) -> ContextBlock:
    content = outline
    while content and budget.estimate_tokens(content) > token_cap:
        content = content[: int(len(content) * 0.9)].rsplit("\n", 1)[0]
    if not content:
        raise ContextPlanError("the paper outline does not fit the context token budget")
    return ContextBlock(
        ContextSource.OUTLINE, f"{_artifact_header(snapshot, 'outline_md')}]", content
    )


def _raw_block(snapshot: PaperSourceSnapshot, section: RetrievedSection) -> ContextBlock:
    title = section.title or "untitled"
    pages = f"{section.pages[0]}-{section.pages[-1]}" if section.pages else "unknown"
    header = f'{_artifact_header(snapshot, "extracted_json")} section="{title}" pages={pages}]'
    return ContextBlock(ContextSource.RAW, header, section.text)
