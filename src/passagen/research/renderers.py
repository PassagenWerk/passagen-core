from __future__ import annotations

import json

from passagen.research.schemas import CollectionSynthesis


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
    lines = ["# Collection Synthesis", "", synthesis.overview, "", "## Themes", ""]
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
