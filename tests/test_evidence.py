from passagen.stages.summarization.evidence import group_by_domain, merge_evidence
from passagen.stages.summarization.schema import EvidenceItem


def item(
    claim: str,
    *,
    category: str = "evaluation_result",
    pages: list[int] | None = None,
    subject: str | None = None,
    subject_value: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        category=category,
        claim=claim,
        evidence_pages=pages or [],
        subject=subject,
        subject_value=subject_value,
    )


def test_merge_deduplicates_identical_claims_and_unites_pages() -> None:
    merged = merge_evidence(
        [
            item("System A doubles throughput.", pages=[7], subject="A", subject_value="2x"),
            item("  System A   doubles throughput. ", pages=[8], subject="A", subject_value="2x"),
        ]
    )

    assert len(merged) == 1
    assert merged[0].evidence_pages == [7, 8]


def test_merge_keeps_conflicting_values_side_by_side() -> None:
    merged = merge_evidence(
        [
            item("System A reaches 10k requests/s.", subject="A", subject_value="10k"),
            item("System A reaches 10k requests/s.", subject="A", subject_value="12k"),
        ]
    )

    assert len(merged) == 2
    assert {entry.subject_value for entry in merged} == {"10k", "12k"}


def test_group_by_domain_maps_categories_to_summary_sections() -> None:
    groups = group_by_domain(
        [
            item("The problem is X.", category="problem"),
            item("The cache layer.", category="design_component"),
            item("10k requests/s.", category="evaluation_result"),
            item("Setup on 8 nodes.", category="evaluation_setup"),
            item("Only LAN tested.", category="limitation"),
            item("Custom category.", category="something_new"),
        ]
    )

    assert set(groups) == {"problem", "design", "evaluation", "discussion"}
    assert len(groups["evaluation"]) == 2
    # Unknown categories fall back to the discussion domain instead of being dropped.
    assert len(groups["discussion"]) == 2
