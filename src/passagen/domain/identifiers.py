import re
from collections import Counter

_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_LINE_BREAK_PATTERN = re.compile(
    r"(?P<prefix>10\.\d{4,9}/[-._;()/:A-Z0-9]+\.)[ \t]*\r?\n[ \t]*"
    r"(?P<suffix>\d{4,}\b)",
    re.IGNORECASE,
)
_ARXIV_PATTERN = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)"
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)",
    re.IGNORECASE,
)
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)


def extract_doi(text: str) -> str | None:
    joined_text = _DOI_LINE_BREAK_PATTERN.sub(r"\g<prefix>\g<suffix>", text)
    candidates = [normalize_doi(match.group(0)) for match in _DOI_PATTERN.finditer(joined_text)]
    if not candidates:
        return None
    counts = Counter(candidates)
    return max(counts, key=lambda candidate: (counts[candidate], len(candidate)))


def extract_arxiv_id(text: str) -> str | None:
    match = _ARXIV_PATTERN.search(text)
    return normalize_arxiv_id(match.group("id")) if match is not None else None


def normalize_doi(value: str) -> str:
    normalized = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix).strip()
    return normalized.rstrip(".,;:)]}")


def normalize_arxiv_id(value: str) -> str:
    normalized = value.strip()
    for prefix in (
        "https://arxiv.org/abs/",
        "http://arxiv.org/abs/",
        "https://arxiv.org/pdf/",
        "http://arxiv.org/pdf/",
        "arxiv:",
    ):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.removesuffix(".pdf")
    return _ARXIV_VERSION.sub("", normalized)
