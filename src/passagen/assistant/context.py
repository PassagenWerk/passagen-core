"""Assemble delimited, budgeted source context for the answer prompt.

Each block carries a stable source header with the citation metadata the model
must copy, so generated citations can be validated against the source snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from passagen.assistant.errors import ContextPlanError
from passagen.assistant.retrieval import RetrievedSection
from passagen.assistant.schemas import (
    ArtifactRef,
    CollectionSourceSnapshot,
    ContextPlan,
    ContextSource,
    Message,
    PaperSourceSnapshot,
    QaRecord,
)
from passagen.providers.budget import TokenBudget
from passagen.stages.summarization.schema import StructuredSummary

_HISTORY_SHARE = 0.15
_SUMMARY_SHARE = 0.45
_OUTLINE_SHARE = 0.20
_RAW_SHARE = 0.40
_PREVIOUS_QA_SHARE = 0.15
_COLLECTION_SUMMARY_SHARE = 0.30
_PAPER_SUMMARIES_SHARE = 0.35


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


def build_collection_context(
    *,
    snapshot: CollectionSourceSnapshot,
    plan: ContextPlan,
    history: Sequence[Message],
    previous_qa: QaRecord | None,
    synthesis_text: str | None,
    summaries: dict[str, StructuredSummary],
    sections: Sequence[RetrievedSection],
    budget: TokenBudget,
    reserved_output_tokens: int,
    prompt_overhead_tokens: int,
) -> AssembledContext:
    """Assemble context for a collection-scoped answer.

    Per-paper summary and raw blocks are emitted only for the papers selected
    in ``plan.paper_ids``; every block header carries the citation metadata of
    its paper snapshot so generated citations stay inside the snapshot.
    """

    available = max(
        0, budget.available_input_tokens(reserved_output_tokens) - prompt_overhead_tokens
    )
    selected = set(plan.paper_ids)
    papers = {paper.paper_id: paper for paper in snapshot.papers}
    unknown = selected - set(papers)
    if unknown:
        raise ContextPlanError(
            "the plan selects papers outside the collection snapshot: " + ", ".join(sorted(unknown))
        )
    blocks: list[ContextBlock] = []
    if ContextSource.CONVERSATION in plan.sources and history:
        blocks.append(_history_block(history, budget, int(available * _HISTORY_SHARE)))
    if ContextSource.PREVIOUS_QA in plan.sources:
        if previous_qa is None or previous_qa.id != plan.reuse_qa_id:
            raise ContextPlanError("the plan requires a matching previous QA record")
        blocks.append(_previous_qa_block(previous_qa, budget, int(available * _PREVIOUS_QA_SHARE)))
    if ContextSource.COLLECTION_SUMMARY in plan.sources and synthesis_text is not None:
        block = _collection_synthesis_block(snapshot, synthesis_text, budget, available)
        if block is not None:
            blocks.append(block)
    if ContextSource.PAPER_SUMMARIES in plan.sources:
        summaries_cap = int(available * _PAPER_SUMMARIES_SHARE)
        for paper in snapshot.papers:
            if paper.paper_id not in selected or paper.paper_id not in summaries:
                continue
            summary = summaries[paper.paper_id]
            content = summary.model_dump_json()
            tokens = budget.estimate_tokens(content)
            if tokens > summaries_cap:
                continue
            blocks.append(
                ContextBlock(
                    ContextSource.PAPER_SUMMARIES,
                    f"{_artifact_header(paper, 'summary_json')}]",
                    content,
                )
            )
            summaries_cap -= tokens
    if ContextSource.RAW in plan.sources:
        raw_cap = int(available * _RAW_SHARE)
        for section in sections:
            section_paper = papers.get(section.paper_id)
            if section_paper is None or section.paper_id not in selected:
                continue
            tokens = budget.estimate_tokens(section.text)
            if tokens > raw_cap:
                continue
            blocks.append(_raw_block(section_paper, section))
            raw_cap -= tokens
    if not blocks:
        raise ContextPlanError("the context plan produced no usable source blocks")
    return AssembledContext(tuple(blocks))


def _collection_synthesis_block(
    snapshot: CollectionSourceSnapshot,
    synthesis_text: str,
    budget: TokenBudget,
    available_tokens: int,
) -> ContextBlock | None:
    artifact = snapshot.synthesis
    if artifact is None:
        return None
    content = synthesis_text
    while content and budget.estimate_tokens(content) > int(
        available_tokens * _COLLECTION_SUMMARY_SHARE
    ):
        content = content[: int(len(content) * 0.9)]
    if not content:
        return None
    header = (
        "[source collection_synthesis "
        f"artifact_id={artifact.artifact_id} artifact_sha256={artifact.sha256}]"
    )
    return ContextBlock(ContextSource.COLLECTION_SUMMARY, header, content)


def build_context(
    *,
    snapshot: PaperSourceSnapshot,
    plan: ContextPlan,
    history: Sequence[Message],
    previous_qa: QaRecord | None,
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
    if ContextSource.PREVIOUS_QA in plan.sources:
        if previous_qa is None or previous_qa.id != plan.reuse_qa_id:
            raise ContextPlanError("the plan requires a matching previous QA record")
        blocks.append(_previous_qa_block(previous_qa, budget, int(available * _PREVIOUS_QA_SHARE)))
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


def _previous_qa_block(record: QaRecord, budget: TokenBudget, token_cap: int) -> ContextBlock:
    content = json.dumps(
        {
            "question": record.standalone_question,
            "answer": record.answer.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )
    if budget.estimate_tokens(content) > token_cap:
        answer = record.answer.answer_markdown
        content = f"question: {record.standalone_question}\nanswer: {answer}"
        while answer and budget.estimate_tokens(content) > token_cap:
            answer = answer[: int(len(answer) * 0.9)]
            content = f"question: {record.standalone_question}\nanswer: {answer}"
    if budget.estimate_tokens(content) > token_cap:
        raise ContextPlanError("the previous QA candidate does not fit the context token budget")
    return ContextBlock(
        ContextSource.PREVIOUS_QA,
        f"[source previous_qa qa_record_id={record.id}]",
        content,
    )


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
