from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from passagen.storage.repository import ArtifactRecord, list_artifacts

_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArtifactIssue:
    artifact: ArtifactRecord
    message: str


@dataclass(slots=True)
class ArtifactCheckResult:
    checked: int = 0
    issues: list[ArtifactIssue] = field(default_factory=list)


def check_artifacts(database_path: Path, data_dir: Path) -> ArtifactCheckResult:
    root = data_dir.expanduser().resolve()
    result = ArtifactCheckResult()
    for artifact in list_artifacts(database_path):
        result.checked += 1
        path = artifact.path
        if path.is_absolute():
            result.issues.append(ArtifactIssue(artifact, "artifact path must be relative"))
            continue
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            result.issues.append(ArtifactIssue(artifact, "artifact path escapes data directory"))
            continue
        if not resolved.is_file():
            result.issues.append(ArtifactIssue(artifact, "artifact file is missing"))
            continue
        size = resolved.stat().st_size
        if artifact.size_bytes is not None and size != artifact.size_bytes:
            result.issues.append(
                ArtifactIssue(
                    artifact,
                    f"size mismatch: expected {artifact.size_bytes}, found {size}",
                )
            )
            continue
        if artifact.sha256 is not None:
            digest = _sha256(resolved)
            if digest != artifact.sha256:
                result.issues.append(
                    ArtifactIssue(
                        artifact,
                        f"SHA-256 mismatch: expected {artifact.sha256}, found {digest}",
                    )
                )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
