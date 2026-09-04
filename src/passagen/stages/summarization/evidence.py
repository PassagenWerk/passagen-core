"""Evidence merging, deduplication, and domain grouping for hierarchical summarization."""

from __future__ import annotations

import re

from passagen.stages.summarization.schema import EvidenceItem

# Coarse domains mirroring the StructuredSummary sections, used for intermediate condensing
# when the merged evidence does not fit the final summary prompt budget.
DOMAIN_BY_CATEGORY = {
    "problem": "problem",
    "motivation": "problem",
    "goal": "problem",
    "non_goal": "problem",
    "assumption": "problem",
    "prior_work_limitation": "problem",
    "contribution": "problem",
    "design_component": "design",
    "process": "design",
    "mechanism": "design",
    "implementation_detail": "design",
    "evaluation_setup": "evaluation",
    "evaluation_result": "evaluation",
    "ablation": "evaluation",
    "limitation": "discussion",
    "tradeoff": "discussion",
    "threat_to_validity": "discussion",
    "conclusion": "discussion",
    "related_work_distinction": "discussion",
}

_WHITESPACE = re.compile(r"\s+")


def merge_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Deduplicate identical claims while keeping conflicting items side by side.

    Items whose normalized claim, category, and quantitative ownership match are merged
    (union of evidence pages); anything that differs is preserved so conflicts reach the
    final summary instead of being silently resolved.
    """
    merged: dict[tuple[str, str, str, str, str, str], EvidenceItem] = {}
    order: list[tuple[str, str, str, str, str, str]] = []
    for item in items:
        key = (
            item.category.strip().lower(),
            _normalize(item.claim),
            (item.subject or "").strip().lower(),
            (item.subject_value or "").strip().lower(),
            (item.baseline or "").strip().lower(),
            (item.baseline_value or "").strip().lower(),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            order.append(key)
            continue
        pages = sorted({*existing.evidence_pages, *item.evidence_pages})
        conditions = list(dict.fromkeys([*existing.conditions, *item.conditions]))
        merged[key] = existing.model_copy(
            update={
                "evidence_pages": pages,
                "conditions": conditions,
                "section": existing.section or item.section,
                "source_excerpt": existing.source_excerpt or item.source_excerpt,
            }
        )
    return [merged[key] for key in order]


def group_by_domain(items: list[EvidenceItem]) -> dict[str, list[EvidenceItem]]:
    groups: dict[str, list[EvidenceItem]] = {}
    for item in items:
        domain = DOMAIN_BY_CATEGORY.get(item.category.strip().lower(), "discussion")
        groups.setdefault(domain, []).append(item)
    return groups


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower())
