"""Semantic chunking of parsed papers along section, paragraph, and sentence boundaries."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from passagen.parsing import ParsedPaper

logger = logging.getLogger(__name__)

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CAPTION = re.compile(r"^(table|figure|fig\.?|listing|algorithm)\s*\d", re.IGNORECASE)
_CAPTION_MAX_CHARACTERS = 400


@dataclass(frozen=True, slots=True)
class PaperChunk:
    index: int
    section_titles: tuple[str, ...]
    pages: tuple[int, ...]
    text: str
    first_unit: int
    last_unit: int
    overlap_units: int


@dataclass(frozen=True, slots=True)
class _Unit:
    section: str
    pages: tuple[int, ...]
    text: str
    is_paragraph: bool


def build_chunks(
    parsed: ParsedPaper,
    *,
    max_input_tokens: int,
    prompt_overhead_tokens: int,
    overlap_paragraphs: int,
    measure: Callable[[str], int],
) -> list[PaperChunk]:
    """Split a parsed paper into budget-limited chunks on semantic boundaries.

    ``measure`` estimates tokens of rendered text. Each rendered chunk, including its
    header, stays within ``max_input_tokens - prompt_overhead_tokens`` so the full facts
    prompt never exceeds the configured chunk input budget.
    """
    limit = max_input_tokens - prompt_overhead_tokens
    if limit <= 0:
        raise ValueError(
            f"chunk_max_input_tokens ({max_input_tokens}) must exceed the facts prompt "
            f"overhead (~{prompt_overhead_tokens} tokens)"
        )
    paper_title = (parsed.metadata.title or "Untitled").strip() or "Untitled"
    units = _paper_units(parsed, paper_title, limit, measure)
    if not units:
        return []

    grouped: list[list[int]] = []
    current: list[int] = []
    for index in range(len(units)):
        candidate = [*current, index]
        if current and measure(_render(paper_title, [units[i] for i in candidate])) > limit:
            grouped.append(current)
            current = [index]
        else:
            current = candidate
    if current:
        grouped.append(current)

    chunks: list[PaperChunk] = []
    for position, indices in enumerate(grouped):
        overlap = 0
        if position > 0 and overlap_paragraphs > 0:
            paragraph_indices = [i for i in grouped[position - 1] if units[i].is_paragraph]
            seed_indices = paragraph_indices[len(paragraph_indices) - overlap_paragraphs :]
            if (
                seed_indices
                and measure(_render(paper_title, [units[i] for i in seed_indices])) <= limit
            ):
                indices = [*seed_indices, *indices]
                overlap = len(seed_indices)
        chunk_units = [units[i] for i in indices]
        chunks.append(
            PaperChunk(
                index=len(chunks) + 1,
                section_titles=_unique_titles(unit.section for unit in chunk_units),
                pages=tuple(sorted({page for unit in chunk_units for page in unit.pages})),
                text=_render(paper_title, chunk_units),
                first_unit=min(indices),
                last_unit=max(indices),
                overlap_units=overlap,
            )
        )
    return chunks


def _paper_units(
    parsed: ParsedPaper,
    paper_title: str,
    limit: int,
    measure: Callable[[str], int],
) -> list[_Unit]:
    units: list[_Unit] = []
    for section in parsed.sections:
        title = (section.title or "Untitled").strip() or "Untitled"
        text = section.text.strip()
        if not text:
            continue
        header_tokens = measure(_render(paper_title, [_Unit(title, section.pages, "", True)]))
        unit_limit = limit - header_tokens
        if unit_limit <= 0:
            raise ValueError("chunk token limit cannot hold a single section header")
        for paragraph in _glued_paragraphs(text):
            units.extend(_paragraph_units(title, section.pages, paragraph, unit_limit, measure))
    return units


def _glued_paragraphs(text: str) -> list[str]:
    """Keep short table/figure captions in the same unit as the following paragraph."""
    paragraphs = [part.strip() for part in _PARAGRAPH_SPLIT.split(text) if part.strip()]
    glued: list[str] = []
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if (
            _CAPTION.match(paragraph)
            and len(paragraph) < _CAPTION_MAX_CHARACTERS
            and index + 1 < len(paragraphs)
        ):
            glued.append(f"{paragraph}\n{paragraphs[index + 1]}")
            index += 2
        else:
            glued.append(paragraph)
            index += 1
    return glued


def _paragraph_units(
    section: str,
    pages: tuple[int, ...],
    paragraph: str,
    limit: int,
    measure: Callable[[str], int],
) -> list[_Unit]:
    if measure(paragraph) <= limit:
        return [_Unit(section, pages, paragraph, True)]
    units: list[_Unit] = []
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(paragraph) if part.strip()]
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip()
        if measure(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            units.append(_Unit(section, pages, buffer, False))
        if measure(sentence) <= limit:
            buffer = sentence
        else:
            logger.warning(
                "semantic unit exceeds chunk budget (%d estimated tokens); hard splitting",
                measure(sentence),
            )
            units.extend(_hard_split_units(section, pages, sentence, limit, measure))
            buffer = ""
    if buffer:
        units.append(_Unit(section, pages, buffer, False))
    return units


def _hard_split_units(
    section: str,
    pages: tuple[int, ...],
    text: str,
    limit: int,
    measure: Callable[[str], int],
) -> list[_Unit]:
    units: list[_Unit] = []
    remaining = text
    while remaining:
        prefix = _fit_prefix(remaining, limit, measure)
        if not prefix:
            prefix = remaining[: max(1, limit)]
        units.append(_Unit(section, pages, prefix.strip(), False))
        remaining = remaining[len(prefix) :].strip()
    return units


def _fit_prefix(text: str, limit: int, measure: Callable[[str], int]) -> str:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _render(paper_title: str, units: list[_Unit]) -> str:
    titles = " > ".join(_unique_titles(unit.section for unit in units))
    pages = sorted({page for unit in units for page in unit.pages})
    header = f"Paper: {paper_title}\nSections: {titles}\nPages: {pages}"
    body = "\n\n".join(unit.text for unit in units if unit.text)
    return f"{header}\n\n{body}\n" if body else f"{header}\n"


def _unique_titles(titles: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for title in titles:
        if title not in result:
            result.append(title)
    return tuple(result)
