"""External metadata service clients: Crossref, arXiv, and GROBID metadata."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from passagen.domain import BibliographicMetadata, normalize_arxiv_id, normalize_doi

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"


class MetadataLookupError(RuntimeError):
    pass


class MetadataLookup(Protocol):
    def lookup(self, identifier: str) -> BibliographicMetadata | None: ...


class PdfMetadataLookup(Protocol):
    def extract(self, path: Path) -> BibliographicMetadata | None: ...


class CrossrefClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        mailto: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.mailto = mailto
        self.client = client

    def lookup(self, identifier: str) -> BibliographicMetadata | None:
        parameters = {"mailto": self.mailto} if self.mailto else None
        try:
            response = self._get(
                f"{self.base_url}/works/{quote(identifier, safe='')}",
                params=parameters,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            document: Any = response.json()
            message = document.get("message") if isinstance(document, dict) else None
            if not isinstance(message, dict):
                raise MetadataLookupError("Crossref response does not contain a message object")
            return _crossref_metadata(message, identifier)
        except MetadataLookupError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise MetadataLookupError(f"Crossref lookup failed for {identifier}: {exc}") from exc

    def _get(self, url: str, *, params: dict[str, str] | None) -> httpx.Response:
        if self.client is not None:
            return self.client.get(url, params=params)
        with httpx.Client(timeout=self.timeout_seconds, headers=_http_headers()) as client:
            return client.get(url, params=params)


class ArxivClient:
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

    def lookup(self, identifier: str) -> BibliographicMetadata | None:
        try:
            response = self._get(
                f"{self.base_url}/api/query",
                params={"id_list": identifier, "max_results": "1"},
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError) as exc:
            raise MetadataLookupError(f"arXiv lookup failed for {identifier}: {exc}") from exc

        entry = root.find(f"{{{_ATOM}}}entry")
        if entry is None:
            return None
        return _arxiv_metadata(entry, identifier)

    def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        if self.client is not None:
            return self.client.get(url, params=params)
        with httpx.Client(timeout=self.timeout_seconds, headers=_http_headers()) as client:
            return client.get(url, params=params)


class GrobidClient:
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

    def is_available(self) -> bool:
        try:
            response = self._get(f"{self.base_url}/api/isalive")
            return response.is_success and response.text.strip().lower() == "true"
        except httpx.HTTPError:
            return False

    def extract(self, path: Path) -> BibliographicMetadata | None:
        try:
            with path.open("rb") as pdf_file:
                response = self._post(
                    f"{self.base_url}/api/processHeaderDocument",
                    files={"input": (path.name, pdf_file, "application/pdf")},
                    data={"consolidateHeader": "0"},
                )
            if response.status_code == 204:
                return None
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (OSError, httpx.HTTPError, ET.ParseError) as exc:
            raise MetadataLookupError(
                f"GROBID header extraction failed for {path.name}: {exc}"
            ) from exc
        return _grobid_metadata(root)

    def _post(
        self,
        url: str,
        *,
        files: dict[str, tuple[str, Any, str]],
        data: dict[str, str],
    ) -> httpx.Response:
        if self.client is not None:
            return self.client.post(url, files=files, data=data)
        with httpx.Client(timeout=self.timeout_seconds, headers=_http_headers()) as client:
            return client.post(url, files=files, data=data)

    def _get(self, url: str) -> httpx.Response:
        if self.client is not None:
            return self.client.get(url)
        with httpx.Client(timeout=self.timeout_seconds, headers=_http_headers()) as client:
            return client.get(url)


def _crossref_metadata(message: dict[str, Any], identifier: str) -> BibliographicMetadata:
    title = _first_string(message.get("title"))
    venue = _first_string(message.get("container-title"))
    authors = _crossref_authors(message.get("author"))
    year = _crossref_year(message)
    doi = normalize_doi(str(message.get("DOI") or identifier))
    source_url = _clean_text(message.get("URL"))
    values: dict[str, object] = {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "source_url": source_url,
    }
    return BibliographicMetadata(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        source_url=source_url,
        sources={name: "crossref" for name, value in values.items() if value},
    )


def _arxiv_metadata(entry: ET.Element, identifier: str) -> BibliographicMetadata:
    title = _element_text(entry, f"{{{_ATOM}}}title")
    authors = tuple(
        name
        for author in entry.findall(f"{{{_ATOM}}}author")
        if (name := _element_text(author, f"{{{_ATOM}}}name")) is not None
    )
    published = _element_text(entry, f"{{{_ATOM}}}published")
    year = int(published[:4]) if published and published[:4].isdigit() else None
    venue = _element_text(entry, f"{{{_ARXIV}}}journal_ref")
    doi_text = _element_text(entry, f"{{{_ARXIV}}}doi")
    doi = normalize_doi(doi_text) if doi_text else None
    source_url = _element_text(entry, f"{{{_ATOM}}}id")
    arxiv_id = normalize_arxiv_id(identifier)
    values: dict[str, object] = {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source_url": source_url,
    }
    return BibliographicMetadata(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        source_url=source_url,
        sources={name: "arxiv" for name, value in values.items() if value},
    )


def _grobid_metadata(root: ET.Element) -> BibliographicMetadata:
    namespace = {"tei": "http://www.tei-c.org/ns/1.0"}
    file_desc = root.find("./tei:teiHeader/tei:fileDesc", namespace)
    bibl_struct = (
        file_desc.find("./tei:sourceDesc/tei:biblStruct", namespace)
        if file_desc is not None
        else None
    )
    analytic = bibl_struct.find("./tei:analytic", namespace) if bibl_struct is not None else None
    title_element = (
        file_desc.find("./tei:titleStmt/tei:title", namespace) if file_desc is not None else None
    )
    if title_element is None and analytic is not None:
        title_element = analytic.find("./tei:title[@type='main']", namespace)
    if title_element is None and analytic is not None:
        title_element = analytic.find("./tei:title", namespace)
    title = _element_content(title_element)
    author_elements = (
        file_desc.findall("./tei:titleStmt/tei:author", namespace) if file_desc is not None else []
    )
    if not author_elements and analytic is not None:
        author_elements = analytic.findall("./tei:author", namespace)
    authors = tuple(
        name
        for author in author_elements
        if (name := _grobid_author(author, namespace)) is not None
    )
    doi = _grobid_identifier(bibl_struct, "doi", namespace)
    arxiv_id = _grobid_identifier(bibl_struct, "arxiv", namespace)
    venue = _element_content(
        bibl_struct.find("./tei:monogr/tei:title", namespace) if bibl_struct is not None else None
    )
    date = (
        bibl_struct.find("./tei:monogr/tei:imprint/tei:date", namespace)
        if bibl_struct is not None
        else None
    )
    if date is None and file_desc is not None:
        date = file_desc.find("./tei:publicationStmt/tei:date", namespace)
    year = _year_from_text(date.get("when") or _element_content(date)) if date is not None else None
    values: dict[str, object] = {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
    }
    return BibliographicMetadata(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        sources={name: "grobid" for name, value in values.items() if value},
    )


def _grobid_author(author: ET.Element, namespace: dict[str, str]) -> str | None:
    person = author.find("./tei:persName", namespace)
    if person is None:
        return _element_content(author)
    parts = [
        content
        for element in person.findall("./tei:forename", namespace)
        if (content := _element_content(element)) is not None
    ]
    surname = _element_content(person.find("./tei:surname", namespace))
    if surname is not None:
        parts.append(surname)
    return _clean_text(" ".join(parts))


def _grobid_identifier(
    bibl_struct: ET.Element | None,
    identifier_type: str,
    namespace: dict[str, str],
) -> str | None:
    if bibl_struct is None:
        return None
    for element in bibl_struct.findall(".//tei:idno", namespace):
        if element.get("type", "").lower() != identifier_type:
            continue
        value = _element_content(element)
        if value is None:
            return None
        return normalize_doi(value) if identifier_type == "doi" else normalize_arxiv_id(value)
    return None


def _crossref_year(message: dict[str, Any]) -> int | None:
    for field_name in ("published-print", "published-online", "published", "issued"):
        value = message.get(field_name)
        if not isinstance(value, dict):
            continue
        date_parts = value.get("date-parts")
        if (
            isinstance(date_parts, list)
            and date_parts
            and isinstance(date_parts[0], list)
            and date_parts[0]
            and isinstance(date_parts[0][0], int)
        ):
            return date_parts[0][0]
    return None


def _first_string(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    return _clean_text(value[0])


def _element_text(element: ET.Element, path: str) -> str | None:
    child = element.find(path)
    return _clean_text(child.text) if child is not None else None


def _element_content(element: ET.Element | None) -> str | None:
    return _clean_text("".join(element.itertext())) if element is not None else None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _year_from_text(value: str | None) -> int | None:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", value or "")
    return int(match.group(0)) if match is not None else None


def _crossref_authors(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    authors: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get(part, "")).strip() for part in ("given", "family")).strip()
        if name:
            authors.append(name)
    return tuple(authors)


def _http_headers() -> dict[str, str]:
    return {"User-Agent": "Passagen/0.1 (local paper metadata tool)"}
