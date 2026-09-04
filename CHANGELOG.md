# Changelog

All notable changes to Passagen Core are documented in this file.

## [Unreleased]

### Added

- Context-budgeted summarization with `auto`, `full`, and `hierarchical` strategies: papers that
  fit the configured context budget are summarized directly from the full serialized text, while
  longer papers fall back to semantic chunking and hierarchical evidence reduction.
- Global LLM budget settings under `providers.llm` (`context_window_tokens`,
  `max_context_utilization`, `safety_margin_tokens`, `chars_per_token`) shared by every LLM call,
  with character-coefficient token estimation and pre-request budget checks.
- Semantic chunk builder that splits on section, paragraph, and sentence boundaries with
  paragraph-level overlap, chunk headers carrying paper title, section path, and pages, and
  table/figure caption gluing.
- Typed `EvidenceItem` schema replacing plain string facts, with merge-time deduplication that
  keeps conflicting values side by side and domain-grouped intermediate condensing when evidence
  exceeds the final summary budget.
- New versioned prompt templates `evidence-v3`, `summary-v3`, `summary-full-v3`, and `reduce-v3`,
  plus `full_prompt_path` and `reduce_prompt_path` overrides.

### Changed

- `pipeline.summarization` now takes `strategy`, `chunk_max_input_tokens`, and
  `chunk_overlap_paragraphs`; the character-based `max_chunk_characters` setting was removed.
- LLM usage stage `fact` was renamed to `evidence`.
- Chunk evidence cache files moved to `papers/<id>/summary/evidence/` and are keyed by prompt and
  chunk content SHA-256; older string-facts caches are not reused.

## [0.2.0] - 2026-09-04

### Added

- Extracted the shared `passagen` domain, storage, provider, processing, and catalog services from
  `passagen-cli` so they can be consumed independently by CLI and Web adapters.
