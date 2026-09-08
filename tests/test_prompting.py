from pathlib import Path

import pytest

from passagen.prompting import PromptTemplateError, load_prompt_template, load_qa_prompt_templates


def test_builtin_prompt_validates_and_renders_declared_variables() -> None:
    template = load_prompt_template(
        "evidence-v3.txt",
        None,
        variables={"schema", "chunk"},
    )

    rendered = template.render(schema='{"type":"object"}', chunk="Section text")

    assert '"type":"object"' in rendered
    assert "Section text" in rendered
    assert len(template.sha256) == 64


def test_prompt_rejects_unknown_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "prompt.txt"
    path.write_text("Schema: $schema\nUnexpected: $other\n")

    with pytest.raises(PromptTemplateError, match="unknown placeholders: other"):
        load_prompt_template(path.name, path, variables={"schema"})


def test_prompt_rejects_invalid_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "prompt.txt"
    path.write_text("Invalid: ${schema\n")

    with pytest.raises(PromptTemplateError, match="invalid placeholder"):
        load_prompt_template(path.name, path, variables={"schema"})


def test_prompt_render_requires_exact_variables() -> None:
    template = load_prompt_template(
        "outline-v2.txt",
        None,
        variables={"schema", "summary"},
    )

    with pytest.raises(PromptTemplateError, match="missing variables: summary"):
        template.render(schema="{}")


def test_qa_answer_prompt_requests_a_detailed_response() -> None:
    templates = load_qa_prompt_templates()

    assert templates.answer.name == "qa-answer-v4.txt"
    assert "Give a thorough, structured answer" in templates.answer.content
    assert "Do not shorten the answer merely for concision" in templates.answer.content
