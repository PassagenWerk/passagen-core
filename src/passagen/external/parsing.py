import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from passagen.domain.identifiers import normalize_arxiv_id, normalize_doi
from passagen.parsing.models import (
    ParsedMetadata,
    ParsedPaper,
    ParsedReference,
    ParsedSection,
    ParsingError,
)

_TEI = "http://www.tei-c.org/ns/1.0"
_NS = {"tei": _TEI}
_COORD_PAGE_PATTERN = re.compile(r"(?:^|;)(?P<page>\d+),")


class GrobidFulltextParser:
    name = "grobid"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.client = client

    def parse(self, path: Path) -> ParsedPaper:
        try:
            with path.open("rb") as pdf_file:
                response = self._post(
                    f"{self.base_url}/api/processFulltextDocument",
                    files={"input": (path.name, pdf_file, "application/pdf")},
                    data={
                        "consolidateHeader": "0",
                        "consolidateCitations": "0",
                        "includeRawCitations": "1",
                        "teiCoordinates": ["head", "p", "biblStruct"],
                    },
                )
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except OSError as exc:
            raise ParsingError("pdf_read_error", f"Cannot read PDF {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ParsingError("grobid_unavailable", f"GROBID parsing failed: {exc}") from exc
        except ET.ParseError as exc:
            raise ParsingError("invalid_tei", f"GROBID returned invalid TEI: {exc}") from exc

        sections = _tei_sections(root)
        if not sections:
            raise ParsingError("insufficient_text", "GROBID returned no body sections")
        return ParsedPaper(
            metadata=_tei_metadata(root),
            sections=tuple(sections),
            references=tuple(_tei_references(root)),
            parser=self.name,
        )

    def _post(
        self,
        url: str,
        *,
        files: dict[str, tuple[str, Any, str]],
        data: dict[str, str | list[str]],
    ) -> httpx.Response:
        if self.client is not None:
            return self.client.post(url, files=files, data=data)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            return client.post(url, files=files, data=data)


def _tei_metadata(root: ET.Element) -> ParsedMetadata:
    title = _content(root.find("./tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:title", _NS))
    analytic = root.find(
        "./tei:teiHeader/tei:fileDesc/tei:sourceDesc/tei:biblStruct/tei:analytic", _NS
    )
    if title is None and analytic is not None:
        title = _content(analytic.find("./tei:title", _NS))
    authors = tuple(
        _person_name(author) for author in root.findall(".//tei:titleStmt/tei:author", _NS)
    )
    if not authors and analytic is not None:
        authors = tuple(_person_name(author) for author in analytic.findall("./tei:author", _NS))
    authors = tuple(author for author in authors if author)
    bibl = root.find("./tei:teiHeader/tei:fileDesc/tei:sourceDesc/tei:biblStruct", _NS)
    venue = _content(bibl.find("./tei:monogr/tei:title", _NS)) if bibl is not None else None
    date = bibl.find("./tei:monogr/tei:imprint/tei:date", _NS) if bibl is not None else None
    doi = _tei_idno(bibl, "doi")
    arxiv_id = _tei_idno(bibl, "arxiv")
    return ParsedMetadata(
        title=title,
        authors=authors,
        year=_year(date.get("when") if date is not None else None),
        venue=venue,
        doi=normalize_doi(doi) if doi else None,
        arxiv_id=normalize_arxiv_id(arxiv_id) if arxiv_id else None,
    )


def _tei_sections(root: ET.Element) -> list[ParsedSection]:
    sections: list[ParsedSection] = []
    for division in root.findall("./tei:text/tei:body//tei:div", _NS):
        paragraphs = division.findall("./tei:p", _NS)
        text = "\n".join(content for paragraph in paragraphs if (content := _content(paragraph)))
        if text:
            sections.append(
                ParsedSection(
                    title=_content(division.find("./tei:head", _NS)),
                    text=text,
                    pages=_pages([division.find("./tei:head", _NS), *paragraphs]),
                )
            )
    return sections


def _tei_references(root: ET.Element) -> list[ParsedReference]:
    references: list[ParsedReference] = []
    for bibl in root.findall(".//tei:listBibl/tei:biblStruct", _NS):
        raw = _content(bibl.find("./tei:note[@type='raw_reference']", _NS)) or _content(bibl)
        if not raw:
            continue
        authors = tuple(
            name for author in bibl.findall(".//tei:author", _NS) if (name := _person_name(author))
        )
        date = bibl.find(".//tei:date", _NS)
        doi = _tei_idno(bibl, "doi")
        references.append(
            ParsedReference(
                raw_text=raw,
                title=_content(bibl.find(".//tei:title[@level='a']", _NS)),
                authors=authors,
                year=_year(date.get("when") if date is not None else None),
                doi=normalize_doi(doi) if doi else None,
            )
        )
    return references


def _pages(elements: list[ET.Element | None]) -> tuple[int, ...]:
    pages: set[int] = set()
    for element in elements:
        if element is not None:
            pages.update(
                int(match.group("page"))
                for match in _COORD_PAGE_PATTERN.finditer(element.get("coords", ""))
            )
    return tuple(sorted(pages))


def _tei_idno(element: ET.Element | None, kind: str) -> str | None:
    if element is not None:
        for idno in element.findall(".//tei:idno", _NS):
            if idno.get("type", "").lower() == kind:
                return _content(idno)
    return None


def _person_name(author: ET.Element) -> str:
    person = author.find("./tei:persName", _NS)
    if person is None:
        return _content(author) or ""
    parts = [_content(item) for item in person.findall("./tei:forename", _NS)]
    parts.append(_content(person.find("./tei:surname", _NS)))
    return " ".join(part for part in parts if part)


def _content(element: ET.Element | None) -> str | None:
    return _clean("".join(element.itertext())) if element is not None else None


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = " ".join(value.split())
    return result or None


def _year(value: str | None) -> int | None:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", value or "")
    return int(match.group(0)) if match else None
