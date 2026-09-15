import httpx
import pytest

from passagen.external.citations import CitationLookupError, DoiCitationClient


def test_doi_client_requests_bibtex_content_negotiation() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="@article{example, title={Paper}}", request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = DoiCitationClient(timeout_seconds=1, client=client).lookup("10.1000/example")

    assert result == "@article{example, title={Paper}}\n"
    assert requests[0].url == "https://doi.org/10.1000/example"
    assert requests[0].headers["accept"] == "application/x-bibtex"


def test_doi_client_rejects_non_bibtex_response() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>Not BibTeX</html>", request=request)
    )
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(CitationLookupError, match="not BibTeX"),
    ):
        DoiCitationClient(timeout_seconds=1, client=client).lookup("10.1000/example")
