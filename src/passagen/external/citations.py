from typing import Protocol
from urllib.parse import quote

import httpx

_MAX_CITATION_BYTES = 1_000_000


class CitationLookupError(RuntimeError):
    pass


class DoiCitationLookup(Protocol):
    def lookup(self, doi: str) -> str | None: ...


class DoiCitationClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.client = client

    def lookup(self, doi: str) -> str | None:
        url = f"https://doi.org/{quote(doi, safe='/')}"
        headers = {
            "Accept": "application/x-bibtex",
            "User-Agent": "Passagen/0.1 (local citation tool)",
        }
        try:
            if self.client is not None:
                response = self.client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                    response = client.get(url, headers=headers)
            if response.status_code in {204, 404, 406}:
                return None
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CitationLookupError(str(exc)) from exc
        content = response.text.strip().replace("\r\n", "\n")
        if len(content.encode("utf-8")) > _MAX_CITATION_BYTES:
            raise CitationLookupError("DOI citation response is too large")
        if not content.startswith("@") or "{" not in content or "}" not in content:
            raise CitationLookupError("DOI citation response is not BibTeX")
        return content + "\n"


__all__ = ["CitationLookupError", "DoiCitationClient", "DoiCitationLookup"]
