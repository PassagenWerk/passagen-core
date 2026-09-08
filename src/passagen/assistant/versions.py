"""Stable version constants for assistant contracts.

Every persisted conversation artifact records the versions that produced it so
reuse and stale decisions can compare semantics instead of guessing from text.
Bump a constant when the corresponding contract changes incompatibly.
"""

from typing import Literal

SNAPSHOT_SCHEMA_VERSION: Literal["1"] = "1"
CONTEXT_PLAN_VERSION: Literal["1"] = "1"
ANSWER_SCHEMA_VERSION: Literal["1"] = "1"
EVAL_SET_SCHEMA_VERSION: Literal["1"] = "1"

CONTEXT_BUILDER_VERSION = "1"
RETRIEVAL_VERSION = "1"
QA_PROMPT_VERSION = "5"
