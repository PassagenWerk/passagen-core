"""Shared fake provider and library builders for collection Core tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from passagen.catalog import CatalogService
from passagen.parsing import ParsedPaper, ParsedSection
from passagen.stages.summarization.schema import (
    EvaluationResult,
    StructuredSummary,
    SummaryEvaluation,
    SummaryIdentity,
)
from passagen.storage.database import connect_database, initialize_database

PAPER_TOPICS = {
    "paper-a": {
        "title": "Fast Scheduler",
        "sections": (
            ParsedSection(
                title="1 Introduction",
                text="Scheduling latency matters for RNIC workloads.",
                pages=(1,),
            ),
            ParsedSection(
                title="4 Evaluation",
                text="We measure tail latency under the RNIC workload. Latency dropped to 12 ms.",
                pages=(5, 6),
            ),
        ),
        "metric": "latency",
        "value": "12 ms",
    },
    "paper-b": {
        "title": "Better Compiler",
        "sections": (
            ParsedSection(
                title="1 Introduction",
                text="Compiler optimization passes improve runtime.",
                pages=(1,),
            ),
            ParsedSection(
                title="3 Evaluation",
                text="Our pass pipeline speeds up kernels by 2.3x on the benchmark suite.",
                pages=(4,),
            ),
        ),
        "metric": "speedup",
        "value": "2.3x",
    },
    "paper-c": {
        "title": "Graph Partitioner",
        "sections": (
            ParsedSection(
                title="1 Introduction",
                text="Graph partitioning enables distributed processing.",
                pages=(1,),
            ),
            ParsedSection(
                title="5 Evaluation",
                text="Partition quality improves edge cut by 18 percent on social graphs.",
                pages=(8,),
            ),
        ),
        "metric": "edge cut",
        "value": "18 percent",
    },
}


@dataclass(frozen=True, slots=True)
class CollectionEnv:
    data_dir: Path
    database_path: Path
    collection_id: str
    paper_ids: tuple[str, ...]
    artifact_ids: dict[str, dict[str, str]]
    artifact_shas: dict[str, dict[str, str]]


def collection_env(
    tmp_path: Path, *, paper_ids: tuple[str, ...] = ("paper-a", "paper-b", "paper-c")
) -> CollectionEnv:
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    initialize_database(database_path)
    artifact_ids: dict[str, dict[str, str]] = {}
    artifact_shas: dict[str, dict[str, str]] = {}
    with connect_database(database_path) as connection:
        for index, paper_id in enumerate(paper_ids):
            topic = PAPER_TOPICS[paper_id]
            parsed = ParsedPaper(sections=topic["sections"], parser="fake")
            summary = StructuredSummary(
                identity=SummaryIdentity(title=str(topic["title"])),
                evaluation=SummaryEvaluation(
                    results=[
                        EvaluationResult(
                            metric=str(topic["metric"]),
                            subject=str(topic["title"]),
                            subject_value=str(topic["value"]),
                            evidence_pages=[int(topic["sections"][1].pages[0])],
                        )
                    ]
                ),
            )
            outline = f"# 1 Introduction\n# {topic['sections'][1].title}\n"
            paper_dir = data_dir / "papers" / paper_id
            paper_dir.mkdir(parents=True)
            contents = {
                "extracted.json": parsed.model_dump_json(),
                "summary.json": summary.model_dump_json(),
                "outline.md": outline,
            }
            for name, content in contents.items():
                (paper_dir / name).write_text(content, encoding="utf-8")

            def sha(name: str, contents: dict[str, str] = contents) -> str:
                return hashlib.sha256(contents[name].encode()).hexdigest()

            connection.execute(
                "INSERT INTO papers (id, title, original_filename, pdf_sha256, status) "
                "VALUES (?, ?, ?, ?, 'outlined')",
                (paper_id, topic["title"], f"{paper_id}.pdf", f"{index:x}".zfill(64)),
            )
            artifact_ids[paper_id] = {}
            artifact_shas[paper_id] = {}
            for kind, name in (
                ("extracted_json", "extracted.json"),
                ("summary_json", "summary.json"),
                ("outline_md", "outline.md"),
            ):
                artifact_id = f"art-{paper_id}-{kind}"
                artifact_ids[paper_id][kind] = artifact_id
                artifact_shas[paper_id][kind] = sha(name)
                connection.execute(
                    "INSERT INTO artifacts (id, paper_id, kind, path, version, sha256) "
                    "VALUES (?, ?, ?, ?, '1', ?)",
                    (artifact_id, paper_id, kind, f"papers/{paper_id}/{name}", sha(name)),
                )
    catalog = CatalogService(database_path, data_dir)
    collection = catalog.create_collection("Systems reading list")
    catalog.add_collection_papers(collection.id, list(paper_ids))
    return CollectionEnv(
        data_dir=data_dir,
        database_path=database_path,
        collection_id=collection.id,
        paper_ids=paper_ids,
        artifact_ids=artifact_ids,
        artifact_shas=artifact_shas,
    )


def summary_citation(
    env: CollectionEnv, paper_id: str, citation_id: str = "c-1"
) -> dict[str, object]:
    return {
        "citation_id": citation_id,
        "paper_id": paper_id,
        "artifact_kind": "summary_json",
        "artifact_id": env.artifact_ids[paper_id]["summary_json"],
        "artifact_sha256": env.artifact_shas[paper_id]["summary_json"],
        "summary_path": "evaluation.results[0]",
        "page_start": PAPER_TOPICS[paper_id]["sections"][1].pages[0],
    }


def raw_citation(env: CollectionEnv, paper_id: str, citation_id: str = "c-1") -> dict[str, object]:
    section = PAPER_TOPICS[paper_id]["sections"][1]
    return {
        "citation_id": citation_id,
        "paper_id": paper_id,
        "artifact_kind": "extracted_json",
        "artifact_id": env.artifact_ids[paper_id]["extracted_json"],
        "artifact_sha256": env.artifact_shas[paper_id]["extracted_json"],
        "section": section.title,
        "page_start": section.pages[0],
        "page_end": section.pages[-1],
    }


def collection_answer(
    env: CollectionEnv,
    question: str,
    citations: list[dict[str, object]] | None = None,
) -> str:
    citations = citations or [summary_citation(env, "paper-a")]
    return json.dumps(
        {
            "standalone_question": question,
            "intent": "fact_lookup",
            "answer_markdown": "Grounded answer ["
            + "] [".join(str(citation["citation_id"]) for citation in citations)
            + "].",
            "claims": [
                {
                    "text": "A grounded claim.",
                    "citation_ids": [str(citation["citation_id"]) for citation in citations],
                }
            ],
            "citations": citations,
            "limitations": [],
            "follow_up_questions": [],
        }
    )


def collection_responder(
    env: CollectionEnv,
    *,
    rewrite_question: str | None = None,
    retrieval_queries: list[str] | None = None,
    citations: list[dict[str, object]] | None = None,
    first_answer: str | None = None,
    repair_answer: str | None = None,
) -> Callable[[str], object]:
    answers = 0
    standalone = ""

    def respond(prompt: str) -> object:
        nonlocal answers, standalone
        if "You rewrite a question" in prompt:
            standalone = rewrite_question or (
                prompt.split("<user_question>", 1)[1].split("</user_question>", 1)[0].strip()
            )
            return json.dumps(
                {
                    "standalone_question": standalone,
                    "retrieval_queries": (
                        retrieval_queries if retrieval_queries is not None else ["latency workload"]
                    ),
                    "requires_exact_quote": False,
                    "conversation_title": "Collection question",
                }
            )
        if "failed validation" in prompt:
            return repair_answer or collection_answer(env, standalone, citations)
        answers += 1
        if first_answer is not None and answers == 1:
            return first_answer
        return collection_answer(env, standalone, citations)

    return respond
