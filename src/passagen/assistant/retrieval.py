"""Raw section retrieval over the parsed paper text.

The protocol exists from the first vertical slice so the in-memory lexical
implementation can later be replaced by a materialized FTS index without
touching the conversation service.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text

from passagen.parsing import ParsedPaper
from passagen.storage.engine import session_scope

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


@dataclass(frozen=True, slots=True)
class PaperCandidate:
    """Compact per-paper document used by bounded lexical paper selection."""

    paper_id: str
    title: str | None
    text: str


def select_papers(
    candidates: list[PaperCandidate],
    queries: list[str],
    *,
    max_papers: int,
) -> list[str]:
    """Choose the most relevant papers for a query with deterministic lexical scoring.

    The selection is bounded by ``max_papers``; when no candidate scores, the
    leading papers in collection order are used so retrieval always has a
    defined, recorded scope.
    """

    if len(candidates) <= max_papers:
        return [candidate.paper_id for candidate in candidates]
    terms = _terms(queries)
    if not terms:
        return [candidate.paper_id for candidate in candidates[:max_papers]]
    scored = [
        (_score(candidate.title or "", candidate.text, terms), -index, candidate.paper_id)
        for index, candidate in enumerate(candidates)
    ]
    scored.sort(reverse=True)
    selected = [paper_id for score, _neg_index, paper_id in scored if score > 0][:max_papers]
    if not selected:
        selected = [candidate.paper_id for candidate in candidates[:max_papers]]
    return selected


class CollectionFtsSectionRetrieval:
    """FTS5 raw-section retrieval constrained to selected papers and snapshot hashes."""

    def __init__(
        self,
        database_path: Path,
        paper_shas: dict[str, str],
        *,
        chars_per_token: float = 4.0,
    ) -> None:
        self.database_path = database_path
        self.paper_shas = dict(paper_shas)
        self.chars_per_token = chars_per_token

    def search(
        self,
        queries: list[str],
        *,
        max_sections: int,
        max_tokens: int,
    ) -> list[RetrievedSection]:
        terms = _terms(queries)
        if not terms or not self.paper_shas:
            return []
        match_query = " OR ".join(f'"{term}"' for term in terms)
        scope_clauses: list[str] = []
        parameters: dict[str, str] = {"query": match_query}
        for index, (paper_id, sha256) in enumerate(sorted(self.paper_shas.items())):
            scope_clauses.append(
                f"(s.paper_id = :paper_{index} AND s.extracted_artifact_sha256 = :sha_{index})"
            )
            parameters[f"paper_{index}"] = paper_id
            parameters[f"sha_{index}"] = sha256
        with session_scope(self.database_path) as session:
            rows = session.execute(
                text(
                    "SELECT s.paper_id, s.ordinal, s.title, s.text, s.pages_json, "
                    "s.extracted_artifact_sha256, bm25(paper_sections_fts, 3.0, 1.0) AS rank "
                    "FROM paper_sections_fts "
                    "JOIN paper_sections AS s ON s.id = paper_sections_fts.rowid "
                    f"WHERE paper_sections_fts MATCH :query AND ({' OR '.join(scope_clauses)}) "
                    "ORDER BY rank, s.paper_id, s.ordinal"
                ),
                parameters,
            ).all()
        return self._select(rows, max_sections=max_sections, max_tokens=max_tokens)

    def _select(
        self, rows: Sequence[Any], *, max_sections: int, max_tokens: int
    ) -> list[RetrievedSection]:
        """Trim ranked rows by paper diversity, relevance, and the global token budget."""

        paper_count = max(1, len(self.paper_shas))
        soft_cap = max(1, math.ceil(max_sections / paper_count))
        selected: list[RetrievedSection] = []
        per_paper: dict[str, int] = {}
        used_tokens = 0
        for allow_over_cap in (False, True):
            for row in rows:
                if len(selected) >= max_sections:
                    return selected
                paper_id = str(row.paper_id)
                if not allow_over_cap and per_paper.get(paper_id, 0) >= soft_cap:
                    continue
                text = str(row.text)
                section_tokens = math.ceil(len(text) / self.chars_per_token)
                if used_tokens + section_tokens > max_tokens:
                    continue
                selected.append(
                    RetrievedSection(
                        paper_id=paper_id,
                        ordinal=int(row.ordinal),
                        title=row.title,
                        text=text,
                        pages=tuple(int(page) for page in json.loads(row.pages_json)),
                        artifact_sha256=str(row.extracted_artifact_sha256),
                        score=-float(row.rank),
                    )
                )
                per_paper[paper_id] = per_paper.get(paper_id, 0) + 1
                used_tokens += section_tokens
        return selected


class FtsSectionRetrieval:
    """SQLite FTS5 retrieval constrained to one immutable extracted artifact."""

    def __init__(
        self,
        database_path: Path,
        paper_id: str,
        *,
        artifact_sha256: str,
        chars_per_token: float = 4.0,
    ) -> None:
        self.database_path = database_path
        self.paper_id = paper_id
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
        match_query = " OR ".join(f'"{term}"' for term in terms)
        with session_scope(self.database_path) as session:
            rows = session.execute(
                text(
                    "SELECT s.ordinal, s.title, s.text, s.pages_json, "
                    "s.extracted_artifact_sha256, bm25(paper_sections_fts, 3.0, 1.0) AS rank "
                    "FROM paper_sections_fts "
                    "JOIN paper_sections AS s ON s.id = paper_sections_fts.rowid "
                    "WHERE paper_sections_fts MATCH :query AND s.paper_id = :paper_id "
                    "AND s.extracted_artifact_sha256 = :sha "
                    "ORDER BY rank, s.ordinal"
                ),
                {"query": match_query, "paper_id": self.paper_id, "sha": self.artifact_sha256},
            ).all()
        selected: list[RetrievedSection] = []
        used_tokens = 0
        for row in rows:
            if len(selected) >= max_sections:
                break
            section_tokens = math.ceil(len(row.text) / self.chars_per_token)
            if used_tokens + section_tokens > max_tokens:
                continue
            selected.append(
                RetrievedSection(
                    paper_id=self.paper_id,
                    ordinal=int(row.ordinal),
                    title=row.title,
                    text=row.text,
                    pages=tuple(int(page) for page in json.loads(row.pages_json)),
                    artifact_sha256=row.extracted_artifact_sha256,
                    score=-float(row.rank),
                )
            )
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
