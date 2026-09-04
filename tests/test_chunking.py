from __future__ import annotations

import pytest

from passagen.parsing import ParsedPaper, ParsedSection
from passagen.stages.summarization.chunking import build_chunks


def measure(text: str) -> int:
    return len(text)


def make_paper(*sections: ParsedSection) -> ParsedPaper:
    return ParsedPaper(parser="test", sections=sections)


def build(parsed: ParsedPaper, *, limit: int = 500, overlap: int = 1):
    return build_chunks(
        parsed,
        max_input_tokens=limit,
        prompt_overhead_tokens=0,
        overlap_paragraphs=overlap,
        measure=measure,
    )


def test_chunks_do_not_split_paragraphs() -> None:
    paragraphs = [f"Paragraph {index} discusses the design. " * 3 for index in range(6)]
    parsed = make_paper(ParsedSection(title="Design", text="\n\n".join(paragraphs), pages=(3, 4)))

    chunks = build(parsed, limit=400, overlap=0)

    assert len(chunks) > 1
    stripped = [paragraph.strip() for paragraph in paragraphs]
    for chunk in chunks:
        body = chunk.text.split("\n\n", 1)[1]
        for paragraph in body.split("\n\n"):
            assert paragraph.strip() in stripped


def test_long_paragraph_splits_on_sentence_boundaries() -> None:
    sentences = [f"Sentence number {index} explains one mechanism." for index in range(20)]
    parsed = make_paper(ParsedSection(title="Design", text=" ".join(sentences), pages=(3,)))

    chunks = build(parsed, limit=300, overlap=0)

    assert len(chunks) > 1
    for chunk in chunks:
        body = chunk.text.split("\n\n", 1)[1].strip()
        assert body.endswith(".")


def test_single_overlong_sentence_is_hard_split() -> None:
    parsed = make_paper(ParsedSection(title="Design", text="word " * 500, pages=(3,)))

    chunks = build(parsed, limit=300, overlap=0)

    assert len(chunks) > 1
    assert all(chunk.overlap_units == 0 for chunk in chunks)


def test_header_carries_paper_section_and_pages() -> None:
    parsed = make_paper(ParsedSection(title="Evaluation", text="Results text.", pages=(7, 8)))

    chunks = build(parsed)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text.startswith("Paper: Untitled\nSections: Evaluation\nPages: [7, 8]")
    assert chunk.section_titles == ("Evaluation",)
    assert chunk.pages == (7, 8)


def test_overlap_repeats_last_paragraph_in_next_chunk() -> None:
    paragraphs = [f"Unique paragraph {index} content here. " * 4 for index in range(8)]
    parsed = make_paper(ParsedSection(title="Design", text="\n\n".join(paragraphs), pages=(3,)))

    chunks = build(parsed, limit=450, overlap=1)

    assert len(chunks) > 1
    assert chunks[1].overlap_units == 1
    first_body_paragraphs = chunks[0].text.split("\n\n")[1:]
    assert first_body_paragraphs[-1].strip() in chunks[1].text


def test_caption_stays_with_following_paragraph() -> None:
    caption = "Table 1: Throughput of all systems."
    explanation = "The table shows that System A doubles throughput. " * 3
    parsed = make_paper(
        ParsedSection(title="Evaluation", text=f"{caption}\n\n{explanation}", pages=(7,))
    )

    chunks = build(parsed, limit=400, overlap=0)

    assert len(chunks) == 1
    assert caption in chunks[0].text
    assert explanation.strip() in chunks[0].text


def test_empty_sections_are_skipped_and_textless_paper_yields_no_chunks() -> None:
    parsed = make_paper(
        ParsedSection(title="Empty", text="   ", pages=(1,)),
        ParsedSection(title="Real", text="Actual content.", pages=(2,)),
    )

    chunks = build(parsed)

    assert len(chunks) == 1
    assert chunks[0].section_titles == ("Real",)

    textless = make_paper(ParsedSection(title="Empty", text="", pages=(1,)))
    assert build(textless) == []


def test_chunk_limit_must_exceed_prompt_overhead() -> None:
    parsed = make_paper(ParsedSection(title="Design", text="Content.", pages=(1,)))

    with pytest.raises(ValueError, match="chunk_max_input_tokens"):
        build_chunks(
            parsed,
            max_input_tokens=100,
            prompt_overhead_tokens=100,
            overlap_paragraphs=0,
            measure=measure,
        )
