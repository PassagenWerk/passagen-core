"""Raw section retrieval over the parsed paper text.

The protocol exists from the first vertical slice so the in-memory lexical
implementation can later be replaced by a materialized FTS index without
touching the conversation service.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol

from passagen.parsing import ParsedPaper

_TERM_PATTERN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "what",
        "which",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class RetrievedSection:
    paper_id: str
    ordinal: int
    title: str | None
    text: str
    pages: tuple[int, ...]
    artifact_sha256: str
    score: float


class SectionRetrieval(Protocol):
    def search(
        self,
        queries: list[str],
        *,
        max_sections: int,
        max_tokens: int,
    ) -> list[RetrievedSection]: ...


class InMemorySectionRetrieval:
    """Lexical section ranking over an already-loaded ``ParsedPaper``."""

    def __init__(
        self,
        paper_id: str,
        parsed: ParsedPaper,
        *,
        artifact_sha256: str,
        chars_per_token: float = 4.0,
    ) -> None:
        self.paper_id = paper_id
        self.parsed = parsed
        self.artifact_sha256 = artifact_sha256
        self.chars_per_token = chars_per_token

    def search(
        self,
        queries: list[str],
        *,
        max_sections: int,
        max_tokens: int,
    ) -> list[RetrievedSection]:
        terms = _terms(queries)
        if not terms:
            return []
        scored: list[RetrievedSection] = []
        for ordinal, section in enumerate(self.parsed.sections):
            score = _score(section.title or "", section.text, terms)
            if score <= 0:
                continue
            scored.append(
                RetrievedSection(
                    paper_id=self.paper_id,
                    ordinal=ordinal,
                    title=section.title,
                    text=section.text,
                    pages=tuple(section.pages),
                    artifact_sha256=self.artifact_sha256,
                    score=score,
                )
            )
        scored.sort(key=lambda section: (-section.score, section.ordinal))
        selected: list[RetrievedSection] = []
        used_tokens = 0
        for candidate in scored:
            if len(selected) >= max_sections:
                break
            section_tokens = math.ceil(len(candidate.text) / self.chars_per_token)
            if used_tokens + section_tokens > max_tokens:
                continue
            selected.append(candidate)
            used_tokens += section_tokens
        return selected


def _terms(queries: list[str]) -> list[str]:
    terms: list[str] = []
    for query in queries:
        for term in _TERM_PATTERN.findall(query.casefold()):
            if len(term) >= 2 and term not in _STOPWORDS and term not in terms:
                terms.append(term)
    return terms


def _score(title: str, text: str, terms: list[str]) -> float:
    folded_title = title.casefold()
    folded_text = text.casefold()
    score = 0.0
    for term in terms:
        score += 3.0 * folded_title.count(term)
        score += float(folded_text.count(term))
    return score
