import re
import statistics
from pathlib import Path
from typing import Any

import pymupdf

from passagen.domain import extract_doi
from passagen.parsing.models import ParsedMetadata, ParsedPaper, ParsedSection, ParsingError

_HEADING_PATTERN = re.compile(r"^(?:\d+(?:\.\d+)*\s+|abstract$|references$)", re.IGNORECASE)


class PyMuPdfParser:
    name = "pymupdf"

    def __init__(self, *, min_text_characters: int) -> None:
        self.min_text_characters = min_text_characters

    def parse(self, path: Path) -> ParsedPaper:
        try:
            with pymupdf.open(path) as document:
                if document.needs_pass:
                    raise ParsingError("encrypted_pdf", f"PDF is encrypted: {path}")
                raw_metadata = document.metadata or {}
                page_lines = [
                    _page_lines(document[index], index + 1) for index in range(document.page_count)
                ]
        except ParsingError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ParsingError("pdf_read_error", f"Cannot parse PDF {path}: {exc}") from exc

        all_text = "\n".join(line[0] for lines in page_lines for line in lines).strip()
        if not all_text:
            raise ParsingError("no_text_layer", f"PDF has no extractable text layer: {path}")
        if len(all_text) < self.min_text_characters:
            raise ParsingError(
                "insufficient_text",
                f"Extracted text is shorter than {self.min_text_characters} characters",
            )
        title = _clean(raw_metadata.get("title")) or _first_line(all_text)
        return ParsedPaper(
            metadata=ParsedMetadata(
                title=title,
                authors=_split_authors(raw_metadata.get("author")),
                year=_year(_clean(raw_metadata.get("creationDate"))),
                doi=extract_doi(all_text),
            ),
            sections=tuple(_layout_sections(page_lines)),
            parser=self.name,
        )


def _page_lines(page: pymupdf.Page, page_number: int) -> list[tuple[str, float, int]]:
    result: list[tuple[str, float, int]] = []
    document: Any = page.get_text("dict")
    for block in document.get("blocks", []):
        if not isinstance(block, dict):
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", []) if isinstance(line, dict) else []
            text = _clean(
                " ".join(str(span.get("text", "")) for span in spans if isinstance(span, dict))
            )
            sizes = [float(span.get("size", 0)) for span in spans if isinstance(span, dict)]
            if text:
                result.append((text, max(sizes, default=0), page_number))
    return result


def _layout_sections(page_lines: list[list[tuple[str, float, int]]]) -> list[ParsedSection]:
    lines = [line for page in page_lines for line in page]
    sizes = [size for _, size, _ in lines if size > 0]
    body_size = statistics.median(sizes) if sizes else 10.0
    sections: list[ParsedSection] = []
    title: str | None = None
    text_lines: list[str] = []
    pages: set[int] = set()

    def flush() -> None:
        if text_lines:
            sections.append(
                ParsedSection(title=title, text="\n".join(text_lines), pages=tuple(sorted(pages)))
            )

    for text, size, page in lines:
        is_heading = len(text) <= 180 and (
            size >= body_size * 1.25 or (_HEADING_PATTERN.match(text) and size >= body_size)
        )
        if is_heading and text_lines:
            flush()
            title = text
            text_lines = []
            pages = set()
        elif is_heading:
            title = text
        else:
            text_lines.append(text)
            pages.add(page)
    flush()
    return sections or [
        ParsedSection(
            text="\n".join(text for text, _, _ in lines),
            pages=tuple(range(1, len(page_lines) + 1)),
        )
    ]


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = " ".join(value.split())
    return result or None


def _year(value: str | None) -> int | None:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", value or "")
    return int(match.group(0)) if match else None


def _split_authors(value: object) -> tuple[str, ...]:
    text = _clean(value)
    if text is None:
        return ()
    separator = ";" if ";" in text else " and "
    return tuple(author.strip() for author in text.split(separator) if author.strip())


def _first_line(text: str) -> str | None:
    return _clean(text.splitlines()[0]) if text else None
