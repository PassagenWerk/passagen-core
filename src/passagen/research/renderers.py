from __future__ import annotations

import json

from passagen.research.schemas import CollectionReport, CollectionSynthesis


def render_synthesis_json(synthesis: CollectionSynthesis) -> bytes:
    return (
        json.dumps(
            synthesis.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def render_synthesis_markdown(synthesis: CollectionSynthesis) -> str:
    lines = ["# Collection Intelligence", "", synthesis.executive_overview, ""]
    lines.extend(["## Paper Roles", ""])
    if synthesis.paper_roles:
        for role in synthesis.paper_roles:
            citations = " ".join(f"[{item}]" for item in role.citation_ids)
            lines.append(f"### {role.paper_id}: {role.role}")
            lines.append("")
            lines.append(f"{role.contribution} {citations}".rstrip())
            if role.method:
                lines.append("")
                lines.append(f"Method: {role.method}")
            lines.append("")
    else:
        lines.extend(["Paper roles were not available in this synthesis version.", ""])
    lines.extend(["## Themes", ""])
    if synthesis.themes:
        for theme in synthesis.themes:
            citations = " ".join(f"[{item}]" for item in theme.citation_ids)
            lines.append(f"### {theme.name}")
            lines.append("")
            lines.append(f"{theme.description} {citations}".rstrip())
            lines.append("")
    else:
        lines.extend(["No common themes were identified.", ""])
    lines.extend(["## Comparison", ""])
    matrix = synthesis.comparison_matrix
    if matrix.dimensions:
        lines.append("| Paper | " + " | ".join(matrix.dimensions) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in matrix.dimensions) + " |")
        for row in matrix.rows:
            by_dimension = {cell.dimension: cell for cell in row.cells}
            values = []
            for dimension in matrix.dimensions:
                cell = by_dimension[dimension]
                references = " ".join(f"[{item}]" for item in cell.citation_ids)
                values.append(f"{cell.value} {references}".replace("|", "\\|").rstrip())
            lines.append(f"| {row.paper_id} | " + " | ".join(values) + " |")
        lines.append("")
    else:
        lines.extend(["No comparison dimensions were identified.", ""])
    for heading, insights in (
        ("Agreements", synthesis.agreements),
        ("Disagreements", synthesis.disagreements),
        ("Complementary Contributions", synthesis.complementary_contributions),
        ("Research Gaps", synthesis.gaps),
    ):
        lines.extend([f"## {heading}", ""])
        if insights:
            for insight in insights:
                references = " ".join(f"[{item}]" for item in insight.citation_ids)
                lines.append(f"- **{insight.name}.** {insight.description} {references}".rstrip())
        else:
            lines.append(f"No {heading.lower()} were identified.")
        lines.append("")
    lines.extend(["## Open Questions", ""])
    if synthesis.open_questions:
        for question in synthesis.open_questions:
            references = " ".join(f"[{item}]" for item in question.citation_ids)
            lines.append(f"- **{question.question}** {question.rationale} {references}".rstrip())
    else:
        lines.append("No grounded follow-up questions were identified.")
    lines.append("")
    lines.extend(["## Claims", ""])
    for claim in synthesis.claims:
        references = " ".join(f"[{item}]" for item in claim.citation_ids)
        lines.append(f"- {claim.text} {references}".rstrip())
    if not synthesis.claims:
        lines.append("No cross-paper claims were identified.")
    lines.extend(["", "## Coverage", ""])
    lines.append("Included papers: " + ", ".join(synthesis.coverage.included_paper_ids))
    if synthesis.coverage.missing_summary_paper_ids:
        lines.append(
            "Missing summaries: " + ", ".join(synthesis.coverage.missing_summary_paper_ids)
        )
    lines.extend(["", "## Citations", ""])
    for citation in synthesis.citations:
        locator = citation.summary_path or citation.section or str(citation.page_start or "")
        lines.append(f"- [{citation.citation_id}] {citation.paper_id}: {locator}")
    return "\n".join(lines).rstrip() + "\n"


def render_report_json(report: CollectionReport) -> bytes:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def render_report_markdown(report: CollectionReport) -> str:
    lines = [f"# {report.title}", ""]
    lines.append(f"Kind: {report.kind.value}")
    if report.user_prompt:
        lines.append(f"Research question: {report.user_prompt}")
    if report.synthesis_artifact_id:
        lines.append(f"Reused collection synthesis: {report.synthesis_artifact_id}")
    lines.append("")
    for section in report.sections:
        lines.append(f"## {section.heading}")
        lines.append("")
        lines.append(section.body_markdown)
        lines.append("")
        for claim in section.claims:
            references = " ".join(f"[{item}]" for item in claim.citation_ids)
            lines.append(f"- {claim.text} {references}".rstrip())
        if section.claims:
            lines.append("")
    if report.claims:
        lines.extend(["## Key Claims", ""])
        for claim in report.claims:
            references = " ".join(f"[{item}]" for item in claim.citation_ids)
            lines.append(f"- {claim.text} {references}".rstrip())
        lines.append("")
    lines.extend(["## Coverage", ""])
    lines.append("Included papers: " + ", ".join(report.coverage.included_paper_ids))
    if report.coverage.missing_summary_paper_ids:
        lines.append("Missing summaries: " + ", ".join(report.coverage.missing_summary_paper_ids))
    lines.extend(["", "## Citations", ""])
    for citation in report.citations:
        locator = citation.summary_path or citation.section or str(citation.page_start or "")
        lines.append(
            f"- [{citation.citation_id}] {citation.paper_id} "
            f"({citation.artifact_kind.value} {citation.artifact_id or ''}): {locator}".rstrip()
        )
    return "\n".join(lines).rstrip() + "\n"
