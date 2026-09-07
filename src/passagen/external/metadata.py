"""External metadata service clients: Crossref, arXiv, and GROBID metadata."""

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from passagen.domain import BibliographicMetadata, normalize_arxiv_id, normalize_doi

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"
_GROBID_COVER_TITLE_PATTERN = re.compile(
    r"^Open access to the Proceedings of .+? is sponsored by\s+",
    re.IGNORECASE,
)


class MetadataLookupError(RuntimeError):
    pass


class MetadataLookup(Protocol):
    def lookup(self, identifier: str) -> BibliographicMetadata | None: ...


class PdfMetadataLookup(Protocol):
    def extract(self, path: Path) -> BibliographicMetadata | None: ...


class CitationMetadataLookup(Protocol):
    def lookup(self, url: str) -> BibliographicMetadata | None: ...


class MetadataSearch(Protocol):
    def search(
        self,
        title: str,
        *,
        authors: tuple[str, ...] = (),
        year: int | None = None,
    ) -> BibliographicMetadata | None: ...


class CitationPageClient:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self.allowed_hosts = frozenset(host.casefold().rstrip(".") for host in allowed_hosts)
        self.timeout_seconds = timeout_seconds
        self.client = client

    def lookup(self, url: str) -> BibliographicMetadata | None:
        if not self.is_allowed(url):
            raise MetadataLookupError(f"Citation page URL is not allowed: {url}")
        try:
            response = self._get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MetadataLookupError(f"Citation page lookup failed for {url}: {exc}") from exc
        if not self.is_allowed(str(response.url)):
            raise MetadataLookupError(
                f"Citation page redirected to a disallowed URL: {response.url}"
            )
        parser = _CitationMetaParser()
        parser.feed(response.text)
        return _citation_page_metadata(parser.values, url)

    def is_allowed(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.casefold().rstrip(".") in self.allowed_hosts
            and parsed.username is None
            and parsed.password is None
            and port in (None, 443)
        )

    def _get(self, url: str) -> httpx.Response:
        if self.client is not None:
            return self.client.get(url)
        with httpx.Client(timeout=self.timeout_seconds, headers=_http_headers()) as client:
            return client.get(url)


class OpenAlexClient:
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

    def search(
        self,
        title: str,
        *,
        authors: tuple[str, ...] = (),
        year: int | None = None,
    ) -> BibliographicMetadata | None:
        parameters = {"search": title, "per-page": "10"}
        if self.mailto:
            parameters["mailto"] = self.mailto
        try:
            response = self._get(f"{self.base_url}/works", params=parameters)
            response.raise_for_status()
            document: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MetadataLookupError(f"OpenAlex search failed for {title!r}: {exc}") from exc
        results = document.get("results") if isinstance(document, dict) else None
        if not isinstance(results, list):
            raise MetadataLookupError("OpenAlex response does not contain a results array")
        matches: list[BibliographicMetadata] = []
        expected_title = _normalized_text(title)
        expected_authors = {_normalized_text(author) for author in authors}
        for result in results:
            if not isinstance(result, dict):
                continue
            metadata = _openalex_metadata(result)
            if _normalized_text(metadata.title) != expected_title:
                continue
            if not expected_authors and year is not None and metadata.year != year:
                continue
            actual_authors = {_normalized_text(author) for author in metadata.authors}
            if expected_authors and not expected_authors & actual_authors:
                continue
            matches.append(metadata)
        return matches[0] if len(matches) == 1 else None

    def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        if self.client is not None:
            return self.client.get(url, params=params)
        with httpx.Client(timeout=self.timeout_seconds, headers=_http_headers()) as client:
            return client.get(url, params=params)


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


class _CitationMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "meta":
            return
        attributes = {name.casefold(): value for name, value in attrs}
        name = attributes.get("name")
        content = _clean_text(attributes.get("content"))
        if name is not None and name.casefold().startswith("citation_") and content is not None:
            self.values.setdefault(name.casefold(), []).append(content)


def _citation_page_metadata(
    values: dict[str, list[str]],
    source_url: str,
) -> BibliographicMetadata | None:
    title = _first_meta(values, "citation_title")
    authors = tuple(values.get("citation_author", ()))
    abstract = _first_meta(values, "citation_abstract")
    year = _year_from_text(
        _first_meta(values, "citation_publication_date") or _first_meta(values, "citation_date")
    )
    venue = next(
        (
            value
            for name in (
                "citation_conference_title",
                "citation_journal_title",
                "citation_book_title",
            )
            if (value := _first_meta(values, name)) is not None
        ),
        None,
    )
    doi_text = _first_meta(values, "citation_doi")
    arxiv_text = _first_meta(values, "citation_arxiv_id")
    if title is None:
        return None
    title = title.replace("{", "").replace("}", "") if title is not None else None
    doi = normalize_doi(doi_text) if doi_text else None
    arxiv_id = normalize_arxiv_id(arxiv_text) if arxiv_text else None
    fields: dict[str, object] = {
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source_url": source_url,
    }
    return BibliographicMetadata(
        title=title,
        abstract=abstract,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        source_url=source_url,
        sources={name: "citation_page" for name, value in fields.items() if value},
    )


def _openalex_metadata(result: dict[str, Any]) -> BibliographicMetadata:
    title = _clean_text(result.get("title") or result.get("display_name"))
    authorships = result.get("authorships")
    authors: list[str] = []
    if isinstance(authorships, list):
        for authorship in authorships:
            author = authorship.get("author") if isinstance(authorship, dict) else None
            name = _clean_text(author.get("display_name")) if isinstance(author, dict) else None
            if name is not None:
                authors.append(name)
    year_value = result.get("publication_year")
    year = year_value if isinstance(year_value, int) else None
    location = result.get("primary_location")
    source = location.get("source") if isinstance(location, dict) else None
    venue = _clean_text(source.get("display_name")) if isinstance(source, dict) else None
    source_url = (
        _clean_text(location.get("landing_page_url")) if isinstance(location, dict) else None
    )
    doi_text = _clean_text(result.get("doi"))
    identifiers = result.get("ids")
    arxiv_text = _clean_text(identifiers.get("arxiv")) if isinstance(identifiers, dict) else None
    doi = normalize_doi(doi_text) if doi_text else None
    arxiv_id = normalize_arxiv_id(arxiv_text) if arxiv_text else None
    abstract = _openalex_abstract(result.get("abstract_inverted_index"))
    fields: dict[str, object] = {
        "title": title,
        "abstract": abstract,
        "authors": tuple(authors),
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source_url": source_url,
    }
    return BibliographicMetadata(
        title=title,
        abstract=abstract,
        authors=tuple(authors),
        year=year,
        venue=venue,
        doi=doi,
        arxiv_id=arxiv_id,
        source_url=source_url,
        sources={name: "openalex" for name, value in fields.items() if value},
    )


def _arxiv_metadata(entry: ET.Element, identifier: str) -> BibliographicMetadata:
    title = _element_text(entry, f"{{{_ATOM}}}title")
    abstract = _element_text(entry, f"{{{_ATOM}}}summary")
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
        "abstract": abstract,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source_url": source_url,
    }
    return BibliographicMetadata(
        title=title,
        abstract=abstract,
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
    title = normalize_grobid_title(_element_content(title_element))
    abstract = _element_content(
        root.find("./tei:teiHeader/tei:profileDesc/tei:abstract", namespace)
    )
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
        "abstract": abstract,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
    }
    return BibliographicMetadata(
        title=title,
        abstract=abstract,
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


def normalize_grobid_title(value: str | None) -> str | None:
    title = _clean_text(value)
    if title is None:
        return None
    return _clean_text(_GROBID_COVER_TITLE_PATTERN.sub("", title))


def _first_meta(values: dict[str, list[str]], name: str) -> str | None:
    candidates = values.get(name)
    return candidates[0] if candidates else None


def _normalized_text(value: str | None) -> str:
    return " ".join(re.findall(r"[^\W_]+", (value or "").casefold()))


def _openalex_abstract(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    positioned_words: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        positioned_words.extend(
            (position, word)
            for position in positions
            if isinstance(position, int) and position >= 0
        )
    if not positioned_words:
        return None
    return _clean_text(" ".join(word for _, word in sorted(positioned_words)))


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
