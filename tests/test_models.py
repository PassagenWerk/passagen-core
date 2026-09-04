from __future__ import annotations

import pytest

from passagen.domain import InvalidStatusTransition, Paper, PaperStatus


def make_paper() -> Paper:
    return Paper(
        original_filename="paper.pdf",
        pdf_sha256="a" * 64,
        file_size_bytes=1024,
    )


def test_paper_id_is_stable_across_metadata_update() -> None:
    paper = make_paper()
    original_id = paper.id

    paper.doi = "10.1000/example"

    assert paper.id == original_id


def test_status_transition_rejects_skipped_stage() -> None:
    paper = make_paper()

    with pytest.raises(InvalidStatusTransition):
        paper.transition_to(PaperStatus.SUMMARIZED)

    paper.transition_to(PaperStatus.METADATA_RESOLVED)
    assert paper.status is PaperStatus.METADATA_RESOLVED

    paper.transition_to(PaperStatus.PARSED)
    assert paper.status is PaperStatus.PARSED
