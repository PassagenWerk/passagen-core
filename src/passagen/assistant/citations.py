"""Validate answer citations against the source snapshot and loaded evidence.

Citations are a contract: every claim must resolve to an artifact inside the
turn's snapshot, and its locator must point at real content. Validation runs
before anything is persisted, so a fabricated citation fails the whole turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from passagen.assistant.errors import CitationValidationError
from passagen.assistant.schemas import (
    ArtifactRef,
    Citation,
    CitationArtifactKind,
    PaperSourceSnapshot,
    StructuredAnswer,
)
from passagen.parsing import ParsedPaper
from passagen.stages.summarization.schema import StructuredSummary

_WHITESPACE = re.compile(r"\s+")
_PATH_SEGMENT = re.compile(r"([^.\[\]]+)(?:\[(\d+)\])?")


@dataclass(frozen=True, slots=True)
class PaperEvidenceIndex:
    """Loaded artifact content used to resolve citation locators."""

    snapshot: PaperSourceSnapshot
    summary: StructuredSummary | None
    outline: str | None
    parsed: ParsedPaper | None


def validate_answer_citations(answer: StructuredAnswer, index: PaperEvidenceIndex) -> None:
    for citation in answer.citations:
        _validate_citation(citation, index)


def _validate_citation(citation: Citation, index: PaperEvidenceIndex) -> None:
    if citation.paper_id != index.snapshot.paper_id:
        raise CitationValidationError(
            f"Citation {citation.citation_id} references paper {citation.paper_id}, "
            f"which is outside the source snapshot ({index.snapshot.paper_id})"
        )
    artifact = _snapshot_artifact(index.snapshot, citation.artifact_kind.value)
    if artifact is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} uses artifact kind {citation.artifact_kind.value}, "
            "which is not part of the source snapshot"
        )
    if citation.artifact_id != artifact.artifact_id:
        raise CitationValidationError(
            f"Citation {citation.citation_id} has artifact_id {citation.artifact_id}, "
            f"expected {artifact.artifact_id}"
        )
    if citation.artifact_sha256 != artifact.sha256:
        raise CitationValidationError(
            f"Citation {citation.citation_id} has a mismatched {citation.artifact_kind.value} hash"
        )
    if citation.artifact_kind is CitationArtifactKind.SUMMARY:
        _validate_summary_locator(citation, index.summary)
    elif citation.artifact_kind is CitationArtifactKind.OUTLINE:
        _validate_outline_locator(citation, index.outline)
    else:
        _validate_raw_locator(citation, index.parsed)


def _snapshot_artifact(snapshot: PaperSourceSnapshot, kind: str) -> ArtifactRef | None:
    for artifact in snapshot.artifacts:
        if artifact.kind == kind:
            return artifact
    return None


def _validate_summary_locator(citation: Citation, summary: StructuredSummary | None) -> None:
    if summary is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} cites the summary, which was not loaded"
        )
    if citation.summary_path is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} requires a summary_path locator"
        )
    node = _resolve_summary_path(summary, citation.summary_path)
    if node is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} summary_path {citation.summary_path} "
            "does not resolve in the paper summary"
        )
    if citation.page_start is not None:
        evidence_pages = node.get("evidence_pages") if isinstance(node, dict) else None
        if not isinstance(evidence_pages, list) or citation.page_start not in evidence_pages:
            rendered_node = json.dumps(node, ensure_ascii=True, sort_keys=True)
            raise CitationValidationError(
                f"Citation {citation.citation_id} page {citation.page_start} is not in the "
                f"evidence pages of {citation.summary_path}. The resolved summary value is "
                f"{rendered_node}; its allowed evidence pages are "
                f"{evidence_pages if isinstance(evidence_pages, list) else []}. Recheck that "
                "the summary_path supports the claim instead of changing only the page."
            )


def _resolve_summary_path(summary: StructuredSummary, path: str) -> object:
    node: object = summary.model_dump(mode="json")
    for segment in path.split("."):
        match = _PATH_SEGMENT.fullmatch(segment)
        if match is None:
            return None
        key, index_text = match.groups()
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
        if index_text is not None:
            if not isinstance(node, list) or int(index_text) >= len(node):
                return None
            node = node[int(index_text)]
    return node


def _validate_outline_locator(citation: Citation, outline: str | None) -> None:
    if outline is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} cites the outline, which was not loaded"
        )
    if citation.section is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} requires a section locator for the outline"
        )
    wanted = _normalize(citation.section)
    headings = [
        _normalize(line.lstrip("#").strip())
        for line in outline.splitlines()
        if line.lstrip().startswith("#")
    ]
    if not any(wanted in heading or heading in wanted for heading in headings if heading):
        raise CitationValidationError(
            f"Citation {citation.citation_id} section {citation.section!r} "
            "does not match any outline heading"
        )


def _validate_raw_locator(citation: Citation, parsed: ParsedPaper | None) -> None:
    if parsed is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} cites raw text, which was not loaded"
        )
    if citation.section is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} requires a section locator for raw text"
        )
    wanted = _normalize(citation.section)
    section = next(
        (
            candidate
            for candidate in parsed.sections
            if candidate.title
            and (wanted in _normalize(candidate.title) or _normalize(candidate.title) in wanted)
        ),
        None,
    )
    if section is None:
        raise CitationValidationError(
            f"Citation {citation.citation_id} section {citation.section!r} "
            "does not match any parsed section"
        )
    if citation.page_start is not None:
        page_end = citation.page_end or citation.page_start
        if not any(citation.page_start <= page <= page_end for page in section.pages):
            raise CitationValidationError(
                f"Citation {citation.citation_id} pages {citation.page_start}-{page_end} "
                f"do not overlap section {section.title!r} pages {list(section.pages)}"
            )
    if citation.excerpt is not None and _normalize(citation.excerpt) not in _normalize(
        section.text
    ):
        raise CitationValidationError(
            f"Citation {citation.citation_id} excerpt was not found in section {section.title!r}. "
            "An excerpt must be one contiguous verbatim substring; replace it with exact source "
            "text or omit the optional excerpt."
        )


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()
