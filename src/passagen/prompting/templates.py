from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from string import Template


class PromptTemplateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    content: str
    sha256: str
    variables: frozenset[str]

    def render(self, **values: str) -> str:
        supplied = set(values)
        if supplied != self.variables:
            missing = sorted(self.variables - supplied)
            unknown = sorted(supplied - self.variables)
            details = []
            if missing:
                details.append(f"missing variables: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown variables: {', '.join(unknown)}")
            raise PromptTemplateError(f"Cannot render prompt {self.name}: {'; '.join(details)}")
        return Template(self.content).substitute(values)


@dataclass(frozen=True, slots=True)
class SummaryPromptTemplates:
    evidence: PromptTemplate
    summary: PromptTemplate
    full: PromptTemplate
    reduce: PromptTemplate
    repair: PromptTemplate


def load_summary_prompt_templates(
    facts_path: Path | None,
    summary_path: Path | None,
    repair_path: Path | None,
    full_path: Path | None = None,
    reduce_path: Path | None = None,
) -> SummaryPromptTemplates:
    return SummaryPromptTemplates(
        evidence=load_prompt_template("evidence-v3.txt", facts_path, variables={"schema", "chunk"}),
        summary=load_prompt_template(
            "summary-v3.txt",
            summary_path,
            variables={"schema", "identity", "evidence"},
        ),
        full=load_prompt_template(
            "summary-full-v3.txt",
            full_path,
            variables={"schema", "identity", "paper"},
        ),
        reduce=load_prompt_template(
            "reduce-v3.txt",
            reduce_path,
            variables={"schema", "evidence"},
        ),
        repair=load_prompt_template(
            "repair-v2.txt",
            repair_path,
            variables={"schema", "validation_error", "candidate"},
        ),
    )


def load_outline_prompt_template(path: Path | None) -> PromptTemplate:
    return load_prompt_template("outline-v2.txt", path, variables={"schema", "summary"})


@dataclass(frozen=True, slots=True)
class QaPromptTemplates:
    rewrite: PromptTemplate
    equivalence: PromptTemplate
    answer: PromptTemplate
    repair: PromptTemplate


def load_qa_prompt_templates(
    rewrite_path: Path | None = None,
    answer_path: Path | None = None,
    repair_path: Path | None = None,
    equivalence_path: Path | None = None,
) -> QaPromptTemplates:
    return QaPromptTemplates(
        rewrite=load_prompt_template(
            "qa-rewrite-v2.txt",
            rewrite_path,
            variables={"schema", "history", "question"},
        ),
        equivalence=load_prompt_template(
            "qa-equivalence-v1.txt",
            equivalence_path,
            variables={"schema", "question", "candidates"},
        ),
        answer=load_prompt_template(
            "qa-answer-v4.txt",
            answer_path,
            variables={"schema", "question", "context"},
        ),
        repair=load_prompt_template(
            "qa-repair-v3.txt",
            repair_path,
            variables={"schema", "question", "context", "validation_error", "candidate"},
        ),
    )


def load_abstract_fix_prompt_template(path: Path | None) -> PromptTemplate:
    return load_prompt_template(
        "abstract-fix-v1.txt",
        path,
        variables={"schema", "title", "abstract"},
    )


def load_prompt_template(
    builtin_name: str,
    override_path: Path | None,
    *,
    variables: set[str],
) -> PromptTemplate:
    name = str(override_path) if override_path is not None else builtin_name
    try:
        content = (
            override_path.expanduser().read_text(encoding="utf-8")
            if override_path is not None
            else files("passagen.resources.prompts")
            .joinpath(builtin_name)
            .read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise PromptTemplateError(f"Cannot read prompt template {name}: {exc}") from exc
    template = Template(content)
    if not template.is_valid():
        raise PromptTemplateError(f"Prompt template {name} contains an invalid placeholder")
    identifiers = set(template.get_identifiers())
    if identifiers != variables:
        missing = sorted(variables - identifiers)
        unknown = sorted(identifiers - variables)
        details = []
        if missing:
            details.append(f"missing placeholders: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown placeholders: {', '.join(unknown)}")
        raise PromptTemplateError(f"Invalid prompt template {name}: {'; '.join(details)}")
    return PromptTemplate(
        name=name,
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        variables=frozenset(variables),
    )
