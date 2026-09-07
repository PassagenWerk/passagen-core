from __future__ import annotations

from passagen.assistant.retrieval import InMemorySectionRetrieval
from passagen.parsing import ParsedPaper, ParsedSection

SHA = "a" * 64


def _parsed() -> ParsedPaper:
    return ParsedPaper(
        sections=(
            ParsedSection(title="1 Introduction", text="Scheduling latency matters.", pages=(1,)),
            ParsedSection(
                title="4.2 Latency Evaluation",
                text="We measure tail latency under the RNIC workload. Latency dropped to 12 ms.",
                pages=(5, 6),
            ),
            ParsedSection(
                title="5 Related Work",
                text="Prior schedulers are surveyed here.",
                pages=(7,),
            ),
        ),
        parser="fake",
    )


def _retrieval() -> InMemorySectionRetrieval:
    return InMemorySectionRetrieval("paper-1", _parsed(), artifact_sha256=SHA)


def test_ranks_sections_by_query_term_overlap() -> None:
    sections = _retrieval().search(["latency workload"], max_sections=3, max_tokens=10_000)

    assert [section.ordinal for section in sections] == [1, 0]
    assert sections[0].pages == (5, 6)
    assert sections[0].artifact_sha256 == SHA


def test_zero_score_sections_are_excluded() -> None:
    sections = _retrieval().search(["nonexistentterm"], max_sections=3, max_tokens=10_000)

    assert sections == []


def test_stopwords_are_ignored() -> None:
    sections = _retrieval().search(["what is the"], max_sections=3, max_tokens=10_000)

    assert sections == []


def test_max_sections_caps_results() -> None:
    sections = _retrieval().search(["latency"], max_sections=1, max_tokens=10_000)

    assert len(sections) == 1
    assert sections[0].title == "4.2 Latency Evaluation"


def test_token_budget_limits_selected_sections() -> None:
    budget_tokens = len("Scheduling latency matters.") // 4 + 1

    sections = _retrieval().search(["latency"], max_sections=3, max_tokens=budget_tokens)

    assert [section.ordinal for section in sections] == [0]
