"""Local PDF metadata extraction using PyMuPDF layout analysis."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from passagen.domain import (
    BibliographicMetadata,
    extract_arxiv_id,
    extract_doi,
    normalize_arxiv_id,
)

_BARE_ARXIV_PATTERN = re.compile(
    r"^(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)$",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_AFFILIATION_WORDS = {
    "academy",
    "alibaba",
    "amazon",
    "cloud",
    "college",
    "company",
    "corporation",
    "department",
    "google",
    "ibm",
    "inc",
    "institute",
    "laboratory",
    "labs",
    "meta",
    "microsoft",
    "research",
    "school",
    "university",
}


class PdfMetadataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _TextSpan:
    page: int
    text: str
    size: float
    x0: float
    y0: float
    y1: float


@dataclass(frozen=True, slots=True)
class _TitleCandidate:
    text: str
    page: int
    size: float
    y1: float
    score: float


def extract_pdf_metadata(
    path: Path,
    *,
    first_pages: int,
    filename_hint: str | None = None,
) -> BibliographicMetadata:
    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise PdfMetadataError(f"PDF is encrypted: {path}")
            raw_metadata = document.metadata or {}
            spans = _extract_spans(document, first_pages)
            text = "\n".join(
                str(document[index].get_text("text"))
                for index in range(min(first_pages, document.page_count))
            )
    except PdfMetadataError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PdfMetadataError(f"Cannot read PDF metadata from {path}: {exc}") from exc

    metadata_text = "\n".join(str(value) for value in raw_metadata.values() if value)
    searchable_text = f"{metadata_text}\n{text}"
    filename_title = _title_from_filename(filename_hint)
    title_candidate = _layout_title(spans, filename_title)
    embedded_title = _clean_text(raw_metadata.get("title"))
    title = (
        embedded_title
        if _is_usable_embedded_title(embedded_title)
        else title_candidate.text
        if title_candidate is not None
        else filename_title or _first_text_line(text)
    )
    authors = _parse_authors(raw_metadata.get("author"))
    if not authors and title_candidate is not None:
        authors = _layout_authors(spans, title_candidate)
    year = _extract_year(raw_metadata.get("creationDate"))
    doi = extract_doi(searchable_text)
    arxiv_id = extract_arxiv_id(searchable_text) or _extract_bare_arxiv_id(path.stem)
    venue = _extract_venue(text)
    source_url = _extract_source_url(text)
    values: dict[str, object] = {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source_url": source_url,
    }
    sources = {name: "pdf" for name, value in values.items() if value}
    return BibliographicMetadata(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        source_url=source_url,
        sources=sources,
    )


def _extract_bare_arxiv_id(value: str) -> str | None:
    match = _BARE_ARXIV_PATTERN.fullmatch(value.strip())
    return normalize_arxiv_id(match.group("id")) if match is not None else None


def _extract_spans(document: pymupdf.Document, first_pages: int) -> list[_TextSpan]:
    spans: list[_TextSpan] = []
    for page_index in range(min(first_pages, document.page_count)):
        page_dict: Any = document[page_index].get_text("dict")
        blocks = page_dict.get("blocks", []) if isinstance(page_dict, dict) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for line in block.get("lines", []):
                if not isinstance(line, dict):
                    continue
                for raw_span in line.get("spans", []):
                    if not isinstance(raw_span, dict):
                        continue
                    text = _clean_text(raw_span.get("text"))
                    bbox = raw_span.get("bbox")
                    size = raw_span.get("size")
                    if (
                        text is None
                        or not isinstance(bbox, (list, tuple))
                        or len(bbox) != 4
                        or not isinstance(size, (int, float))
                    ):
                        continue
                    spans.append(
                        _TextSpan(
                            page=page_index,
                            text=text,
                            size=float(size),
                            x0=float(bbox[0]),
                            y0=float(bbox[1]),
                            y1=float(bbox[3]),
                        )
                    )
    return spans


def _layout_title(spans: list[_TextSpan], filename_title: str | None) -> _TitleCandidate | None:
    candidates: list[_TitleCandidate] = []
    for page in sorted({span.page for span in spans}):
        page_spans = [span for span in spans if span.page == page and len(span.text) > 1]
        font_sizes = sorted({round(span.size, 1) for span in page_spans}, reverse=True)[:3]
        for font_size in font_sizes:
            same_size = [span for span in page_spans if abs(span.size - font_size) <= 0.3]
            for group in _contiguous_span_groups(same_size):
                text = _clean_text(" ".join(span.text for span in group))
                if text is None or len(_words(text)) < 3 or _URL_PATTERN.search(text):
                    continue
                overlap = _word_overlap(text, filename_title) if filename_title else 0.0
                score = font_size + overlap * 100 + min(len(_words(text)), 10)
                candidates.append(
                    _TitleCandidate(
                        text=text,
                        page=page,
                        size=font_size,
                        y1=max(span.y1 for span in group),
                        score=score,
                    )
                )
    return max(candidates, key=lambda candidate: candidate.score, default=None)


def _contiguous_span_groups(spans: list[_TextSpan]) -> list[list[_TextSpan]]:
    groups: list[list[_TextSpan]] = []
    for span in sorted(spans, key=lambda item: (item.y0, item.x0)):
        if not groups:
            groups.append([span])
            continue
        previous_bottom = max(item.y1 for item in groups[-1])
        if span.y0 - previous_bottom <= max(span.size * 1.4, 8):
            groups[-1].append(span)
        else:
            groups.append([span])
    return groups


def _layout_authors(spans: list[_TextSpan], title: _TitleCandidate) -> tuple[str, ...]:
    author_spans = [
        span
        for span in spans
        if span.page == title.page
        and title.y1 - 1 <= span.y0 <= title.y1 + 90
        and max(8, title.size * 0.5) <= span.size < title.size - 1
        and not _URL_PATTERN.search(span.text)
    ]
    text = " ".join(span.text for span in sorted(author_spans, key=lambda item: (item.y0, item.x0)))
    return _parse_layout_authors(text)


def _parse_layout_authors(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+and\s+", ", ", text, flags=re.IGNORECASE)
    authors: list[str] = []
    for part in normalized.split(","):
        candidate = re.sub(r"^[\d*†‡§\s]+|[\d*†‡§\s]+$", "", part).strip()
        words = candidate.split()
        lowered = {re.sub(r"[^a-z]", "", word.lower()) for word in words}
        if (
            not 2 <= len(words) <= 5
            or lowered & _AFFILIATION_WORDS
            or not all(_looks_like_name_word(word) for word in words)
        ):
            continue
        authors.append(candidate)
    return tuple(dict.fromkeys(authors))


def _looks_like_name_word(word: str) -> bool:
    cleaned = word.strip(".-'’")
    return (
        bool(cleaned) and cleaned[0].isupper() and any(character.isalpha() for character in cleaned)
    )


def _title_from_filename(filename: str | None) -> str | None:
    if filename is None:
        return None
    title = Path(filename).stem.replace("_", " ")
    if " - " in title:
        prefix, candidate = title.split(" - ", maxsplit=1)
        if "et al" in prefix.lower() or len(prefix.split()) <= 6:
            title = candidate
    return _clean_text(title)


def _is_usable_embedded_title(title: str | None) -> bool:
    if title is None or len(_words(title)) < 2:
        return False
    lowered = title.lower()
    rejected = ("untitled", "microsoft word", "this paper is included in the")
    return not any(value in lowered for value in rejected)


def _word_overlap(left: str, right: str | None) -> float:
    if right is None:
        return 0.0
    left_words = _words(left)
    right_words = _words(right)
    all_words = left_words | right_words
    return len(left_words & right_words) / len(all_words) if all_words else 0.0


def _words(value: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9]+", value) if len(word) > 1}


def _extract_venue(text: str) -> str | None:
    normalized = " ".join(text.split())
    match = re.search(
        r"Proceedings of (?:the )?(?P<venue>.+?)(?:\.\s|\b(?:January|February|March|"
        r"April|May|June|July|August|September|October|November|December)\b)",
        normalized,
        re.IGNORECASE,
    )
    return _clean_text(match.group("venue")) if match is not None else None


def _extract_source_url(text: str) -> str | None:
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:)]}")
        lowered = url.lower()
        if "doi.org/" not in lowered and "arxiv.org/" not in lowered:
            return url
    return None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _parse_authors(value: object) -> tuple[str, ...]:
    text = _clean_text(value)
    if text is None:
        return ()
    separator = ";" if ";" in text else " and "
    return tuple(author.strip() for author in text.split(separator) if author.strip())


def _first_text_line(text: str) -> str | None:
    for line in text.splitlines():
        normalized = _clean_text(line)
        if normalized:
            return normalized[:500]
    return None


def _extract_year(value: object) -> int | None:
    text = _clean_text(value)
    if text is None:
        return None
    match = re.search(r"(?:D:)?(?P<year>19\d{2}|20\d{2})", text)
    return int(match.group("year")) if match is not None else None
