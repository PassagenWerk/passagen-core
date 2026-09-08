# Changelog

All notable changes to Passagen Core are documented in this file.

## [Unreleased]

### Changed

- Aligned the default context and generation budgets with the 1M-context
  `deepseek-flash-v4` default model.
- Updated the built-in QA answer prompt to request thorough, structured responses and removed
  obsolete bundled QA prompt versions.
- Raised the storage schema to version 10 for conversation, section-index, collection-artifact,
  and report persistence.

### Fixed

- Answer generation now routes schema validation failures (for example a citation without a
  summary path, section, or page locator) through the same bounded repair pass as citation
  content failures instead of failing the turn immediately; the QA answer/repair prompts (v2)
  explicitly require per-artifact-kind locators, repair receives the allowed source context,
  and `QA_PROMPT_VERSION` is now `2`.
- Turn submission now persists its run and adjacent user/assistant messages in one transaction;
  queued turns cannot consume later questions as conversation history, and run claiming uses one
  conditional database update.
- Queued turns reject changed sources instead of attributing newly rebuilt artifacts to an old
  source snapshot.
- Service restart recovery now marks pending answer messages failed alongside interrupted runs,
  archive search includes tags, model-returned question/intent metadata is normalized to the
  planner result, and answer/repair calls enforce their configured context budget.

### Added

- Persistent paper and collection conversations with versioned message, QA, citation, source
  snapshot, and context-plan contracts. Answers can combine conversation history, previous QA,
  Summary, Outline, and selected raw sections while validating every cited artifact, locator,
  page range, and source hash.
- Asynchronous question turns with atomic submission and completion, per-stage LLM accounting,
  prompt/response diagnostics, stable failure states, and retry without saving partial answers.
- Structured answer archives with titles, tags, escaped search, full JSON export, and generated
  versus reused provenance.
- Full-text section retrieval backed by a transactionally maintained SQLite FTS5 index, with lazy
  indexing for existing libraries. Chinese questions can use rewritten English retrieval terms to
  locate English paper sections.
- Duplicate question reuse constrained by scope, normalized question, source fingerprint, and
  prompt/schema compatibility. Compatible exact questions skip the answer model; conservative
  semantic matches reuse only high-confidence equivalents, while partial matches become previous
  QA context for a newly generated answer. Callers can force regeneration, inspect reuse
  provenance, and receive structured stale reasons.
- Collection synthesis and comparison artifacts with ordered source fingerprints, complete or
  explicitly partial coverage, bounded summary-only direct and map/reduce generation, validated
  cross-paper citations, deterministic JSON/Markdown output, source manifests, reuse, force
  regeneration, and stale detection.
- Collection reports for review, comparison, research gaps, and custom questions, including
  versioned structured output, citation navigation metadata, bounded repair, run and artifact
  lifecycle persistence, synthesis reuse when source versions match, and explicit partial or stale
  coverage.
- Multi-paper collection context planning and two-level retrieval: bounded paper selection from
  collection synthesis and per-paper summaries, followed by global-budget FTS5 section search
  limited to the selected papers and their snapshotted artifact hashes.
- A shared generation-run dispatcher that claims and executes answer, synthesis, and report runs
  with consistent lifecycle and token accounting, and interrupt recovery that prevents answers,
  syntheses, and reports from remaining permanently running after a restart.

## [0.5.0] - 2026-09-05

### Added

- Canonical paper abstracts with schema version 4 storage, arXiv and GROBID metadata support,
  full-text parser extraction, source tracking, and user-edit precedence.
- An explicit, non-blocking `abstract` processing stage that stores validated LLM-cleaned text as
  a separate, hash-keyed artifact while retaining the original author abstract.

### Changed

- Rebuilding from the independent `abstract` stage now refreshes only the cleaned Abstract view;
  Summary, Outline, and PaperStatus remain unchanged.
- Defaulted the OpenAI-compatible LLM configuration to DeepSeek `deepseek-flash-v4` and organized
  user, development, and roadmap documentation by audience and delivery status, with
  forge-neutral cross-repository links.

## [0.4.0] - 2026-09-05

### Added

- `TagUsage` projection and `CatalogService.list_tag_usage()` returning per-tag paper counts from
  a single aggregate query.

### Changed

- `PaperFilters` replaces the single `tag_id` with `tag_ids` plus a `tag_match` mode (`all`
  requires every selected tag, `any` accepts papers carrying at least one); multi-tag filtering
  keeps result totals, ordering, and pagination exact.

## [0.3.1] - 2026-09-04

### Fixed

- Excluded the repository-local uv cache from source distributions so tagged CI builds do not
  package virtual-environment symlinks.

## [0.3.0] - 2026-09-04

### Added

- `passagen.processing`: adapter-independent `ProcessingService` with a persisted
  `ProcessingRun`/`ProgressEvent` contract (new `update_runs` table, schema version 3),
  per-paper conflict detection, restart interruption recovery, structured progress events under
  `data/runs/<run-id>/events.jsonl`, and a per-run configuration snapshot in
  `data/runs/<run-id>/run.json`.
- `update_papers` now accepts an explicit `paper_ids` selection, a `from_stage` rebuild point
  (`metadata`, `parse`, `summary`, `outline`), structured `UpdateEvent` callbacks, and records a
  stable failure category per failed paper.
- `passagen.stages.scanning.import_files` imports explicit PDF files (e.g. browser uploads) with
  the same content-addressed deduplication as directory scans, isolating per-file failures with a
  stable reason code.
- `load_settings()` resolves relative prompt paths against the config file directory and exposes
  `resolve_config_path()` so adapters can log the configuration actually in use.
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
