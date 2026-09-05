from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from passagen.config import MetadataSettings, ProvidersSettings
from passagen.domain import PaperStatus
from passagen.providers import ProviderHealthSnapshot, ProviderUnavailableError
from passagen.providers.metadata import (
    BibliographicMetadata,
    ConfiguredMetadataProvider,
    MetadataLookup,
    MetadataLookupError,
    PdfMetadataError,
    PdfMetadataLookup,
    extract_pdf_metadata,
    merge_metadata,
)
from passagen.stages.metadata.models import MetadataResolutionError, MetadataResolutionResult
from passagen.stages.progress import ProgressCallback, report_progress
from passagen.storage.repository import (
    MetadataConflictError,
    PaperRecord,
    get_paper,
    update_paper_metadata,
    update_paper_status,
)

logger = logging.getLogger(__name__)


def resolve_paper_metadata(
    database_path: Path,
    data_dir: Path,
    paper_id: str,
    settings: MetadataSettings,
    providers: ProvidersSettings,
    *,
    provider_health: ProviderHealthSnapshot | None = None,
    force: bool = False,
    crossref: MetadataLookup | None = None,
    arxiv: MetadataLookup | None = None,
    grobid: PdfMetadataLookup | None = None,
    progress: ProgressCallback | None = None,
) -> MetadataResolutionResult:
    paper = get_paper(database_path, paper_id)
    if paper is None:
        logger.error("metadata failed: paper not found: paper_id=%s", paper_id)
        raise MetadataResolutionError(f"Paper not found: {paper_id}")
    logger.info(
        "metadata started: paper_id=%s status=%s force=%s filename=%s",
        paper.id,
        paper.status.value,
        force,
        paper.original_filename,
    )
    if paper.status is not PaperStatus.DISCOVERED and not force:
        logger.info(
            "metadata skipped: paper_id=%s status=%s reason=already_resolved",
            paper.id,
            paper.status.value,
        )
        return MetadataResolutionResult(paper=paper, updated=False)
    if force and paper.status is not PaperStatus.DISCOVERED:
        update_paper_status(database_path, paper_id, PaperStatus.DISCOVERED)
    if paper.managed_pdf_path is None:
        logger.error("metadata failed: paper_id=%s reason=no_managed_pdf", paper.id)
        raise MetadataResolutionError(f"Paper has no managed PDF artifact: {paper_id}")

    pdf_path = data_dir / paper.managed_pdf_path
    if not pdf_path.is_file():
        logger.error("metadata failed: paper_id=%s missing_pdf=%s", paper.id, pdf_path)
        raise MetadataResolutionError(f"Managed PDF does not exist: {pdf_path}")
    report_progress(progress, f"Reading local PDF metadata: {paper.original_filename}")
    try:
        local = extract_pdf_metadata(
            pdf_path,
            first_pages=settings.first_pages,
            filename_hint=paper.original_filename,
        )
    except PdfMetadataError as exc:
        logger.error("metadata local extraction failed: paper_id=%s error=%s", paper.id, exc)
        raise MetadataResolutionError(str(exc)) from exc

    logger.info(
        "metadata local extraction succeeded: paper_id=%s title=%r authors=%s year=%s "
        "doi=%s arxiv_id=%s venue=%r",
        paper.id,
        local.title,
        len(local.authors),
        local.year,
        local.doi,
        local.arxiv_id,
        local.venue,
    )
    report_progress(progress, "Local PDF metadata extracted.")

    warnings: list[str] = []
    metadata_provider = ConfiguredMetadataProvider(
        providers,
        crossref=crossref,
        arxiv=arxiv,
        grobid=grobid,
    )
    grobid_attempted = False
    grobid_metadata = BibliographicMetadata()
    if _needs_grobid(local):
        _require_provider(provider_health, "grobid")
        fallback_reason = _grobid_reason(local)
        logger.info(
            "metadata fallback selected: paper_id=%s provider=GROBID reason=%s",
            paper.id,
            fallback_reason,
        )
        report_progress(progress, f"Trying GROBID fallback ({fallback_reason}).")
        extracted = _extract_grobid(pdf_path, metadata_provider.grobid, warnings, progress)
        grobid_metadata = _initial_grobid_fallback(local, extracted, warnings, progress)
        grobid_attempted = True
    candidate = merge_metadata(local, grobid_metadata)
    queried_doi = candidate.doi
    if queried_doi is not None and providers.crossref.enabled:
        _require_provider(provider_health, "crossref")
    crossref_metadata = _lookup(
        "Crossref",
        queried_doi,
        metadata_provider.crossref,
        enabled=providers.crossref.enabled,
        warnings=warnings,
        progress=progress,
    )

    if not _titles_match(candidate.title, crossref_metadata.title) and not grobid_attempted:
        _require_provider(provider_health, "grobid")
        logger.warning(
            "metadata Crossref title conflict: paper_id=%s doi=%s; trying GROBID fallback",
            paper.id,
            queried_doi,
        )
        report_progress(progress, "Crossref title conflict; trying GROBID fallback.")
        extracted = _extract_grobid(pdf_path, metadata_provider.grobid, warnings, progress)
        if _titles_match(crossref_metadata.title, extracted.title):
            grobid_metadata = extracted
        else:
            _reject_grobid_title(
                "Crossref",
                crossref_metadata.title,
                extracted.title,
                warnings,
                progress,
            )
            grobid_metadata = BibliographicMetadata()
        grobid_attempted = True
        candidate = merge_metadata(local, grobid_metadata)
        if candidate.doi != queried_doi:
            logger.info(
                "metadata DOI corrected by GROBID: paper_id=%s old_doi=%s new_doi=%s",
                paper.id,
                queried_doi,
                candidate.doi,
            )
            queried_doi = candidate.doi
            crossref_metadata = _lookup(
                "Crossref",
                queried_doi,
                metadata_provider.crossref,
                enabled=providers.crossref.enabled,
                warnings=warnings,
                progress=progress,
            )

    if not _titles_match(candidate.title, crossref_metadata.title):
        logger.warning(
            "metadata Crossref response rejected: paper_id=%s doi=%s expected_title=%r "
            "actual_title=%r",
            paper.id,
            queried_doi,
            candidate.title,
            crossref_metadata.title,
        )
        warnings.append(
            f"Crossref title does not match PDF title for {queried_doi}; ignoring response"
        )
        crossref_metadata = BibliographicMetadata()
    if candidate.arxiv_id is not None and providers.arxiv.enabled:
        _require_provider(provider_health, "arxiv")
    arxiv_metadata = _lookup(
        "arXiv",
        candidate.arxiv_id,
        metadata_provider.arxiv,
        enabled=providers.arxiv.enabled,
        warnings=warnings,
        progress=progress,
    )
    existing = _existing_metadata(paper)
    metadata = merge_metadata(
        local,
        grobid_metadata,
        arxiv_metadata,
        crossref_metadata,
        existing,
    )
    report_progress(progress, "Saving resolved metadata...")
    try:
        updated = update_paper_metadata(
            database_path, paper_id, metadata, PaperStatus.METADATA_RESOLVED
        )
    except MetadataConflictError as exc:
        logger.error("metadata persistence failed: paper_id=%s error=%s", paper.id, exc)
        raise MetadataResolutionError(str(exc)) from exc
    logger.info(
        "metadata finished: paper_id=%s status=%s title=%r doi=%s arxiv_id=%s sources=%s",
        paper.id,
        updated.status.value,
        updated.title,
        updated.doi,
        updated.arxiv_id,
        updated.metadata_sources,
    )
    report_progress(progress, "Metadata saved.")
    return MetadataResolutionResult(paper=updated, warnings=tuple(warnings))


def _require_provider(health: ProviderHealthSnapshot | None, name: str) -> None:
    if health is None:
        return
    try:
        health.require(name)
    except ProviderUnavailableError as exc:
        raise MetadataResolutionError(str(exc)) from exc


def _lookup(
    provider: str,
    identifier: str | None,
    lookup: Callable[[str], BibliographicMetadata | None],
    *,
    enabled: bool,
    warnings: list[str],
    progress: ProgressCallback | None,
) -> BibliographicMetadata:
    route = "doi" if provider == "Crossref" else "arxiv"
    if not enabled:
        logger.info(
            "metadata route skipped: provider=%s reason=disabled route=%s",
            provider,
            route,
        )
        return BibliographicMetadata()
    if identifier is None:
        logger.info(
            "metadata route skipped: provider=%s reason=no_identifier route=%s",
            provider,
            route,
        )
        return BibliographicMetadata()
    logger.info(
        "metadata route selected: provider=%s identifier=%s route=%s",
        provider,
        identifier,
        route,
    )
    if provider == "Crossref":
        report_progress(progress, f"Querying Crossref by DOI: {identifier}")
    else:
        report_progress(progress, f"Querying arXiv by ID: {identifier}")
    try:
        result = lookup(identifier)
    except MetadataLookupError as exc:
        logger.warning(
            "metadata provider failed: provider=%s identifier=%s error=%s",
            provider,
            identifier,
            exc,
        )
        warnings.append(str(exc))
        report_progress(progress, f"{provider} lookup failed; continuing with fallback data.")
        return BibliographicMetadata()
    if result is None:
        logger.warning(
            "metadata provider not found: provider=%s identifier=%s",
            provider,
            identifier,
        )
        warnings.append(f"{provider} did not find metadata for {identifier}")
        report_progress(progress, f"{provider} returned no result; continuing.")
        return BibliographicMetadata()
    logger.info(
        "metadata provider succeeded: provider=%s identifier=%s title=%r doi=%s arxiv_id=%s",
        provider,
        identifier,
        result.title,
        result.doi,
        result.arxiv_id,
    )
    report_progress(progress, f"{provider} metadata received.")
    return result


def _extract_grobid(
    pdf_path: Path,
    extract: Callable[[Path], BibliographicMetadata | None],
    warnings: list[str],
    progress: ProgressCallback | None,
) -> BibliographicMetadata:
    logger.info("metadata route selected: provider=GROBID file=%s", pdf_path)
    report_progress(progress, "Uploading PDF to GROBID...")
    try:
        result = extract(pdf_path)
    except MetadataLookupError as exc:
        logger.warning("metadata provider failed: provider=GROBID file=%s error=%s", pdf_path, exc)
        warnings.append(str(exc))
        report_progress(progress, "GROBID extraction failed; continuing with local metadata.")
        return BibliographicMetadata()
    if result is None:
        logger.warning("metadata provider returned no result: provider=GROBID file=%s", pdf_path)
        warnings.append(f"GROBID did not extract metadata from {pdf_path.name}")
        report_progress(progress, "GROBID returned no metadata; continuing.")
        return BibliographicMetadata()
    logger.info(
        "metadata provider succeeded: provider=GROBID file=%s title=%r doi=%s arxiv_id=%s",
        pdf_path,
        result.title,
        result.doi,
        result.arxiv_id,
    )
    report_progress(progress, "GROBID metadata received.")
    return result


def _needs_grobid(metadata: BibliographicMetadata) -> bool:
    return (
        metadata.title is None
        or not metadata.authors
        or (metadata.doi is None and metadata.arxiv_id is None)
    )


def _initial_grobid_fallback(
    local: BibliographicMetadata,
    extracted: BibliographicMetadata,
    warnings: list[str],
    progress: ProgressCallback | None,
) -> BibliographicMetadata:
    if not _titles_match(local.title, extracted.title):
        _reject_grobid_title("PDF", local.title, extracted.title, warnings, progress)
        return BibliographicMetadata()
    return _missing_metadata(local, extracted)


def _reject_grobid_title(
    expected_source: str,
    expected_title: str | None,
    grobid_title: str | None,
    warnings: list[str],
    progress: ProgressCallback | None,
) -> None:
    warning = f"GROBID title does not match {expected_source} title; ignoring response"
    logger.warning(
        "metadata GROBID response rejected: expected_source=%s expected_title=%r grobid_title=%r",
        expected_source,
        expected_title,
        grobid_title,
    )
    warnings.append(warning)
    report_progress(progress, f"{warning}.")


def _missing_metadata(
    primary: BibliographicMetadata,
    fallback: BibliographicMetadata,
) -> BibliographicMetadata:
    values: dict[str, object] = {}
    for name in (
        "title",
        "abstract",
        "authors",
        "year",
        "venue",
        "doi",
        "arxiv_id",
        "source_url",
    ):
        primary_value = getattr(primary, name)
        fallback_value = getattr(fallback, name)
        if primary_value in (None, (), "") and fallback_value not in (None, (), ""):
            values[name] = fallback_value
    authors_value = values.get("authors")
    year_value = values.get("year")
    return BibliographicMetadata(
        title=str(values["title"]) if "title" in values else None,
        abstract=str(values["abstract"]) if "abstract" in values else None,
        authors=(
            tuple(str(author) for author in authors_value)
            if isinstance(authors_value, tuple)
            else ()
        ),
        year=year_value if isinstance(year_value, int) else None,
        venue=str(values["venue"]) if "venue" in values else None,
        doi=str(values["doi"]) if "doi" in values else None,
        arxiv_id=str(values["arxiv_id"]) if "arxiv_id" in values else None,
        source_url=str(values["source_url"]) if "source_url" in values else None,
        sources={name: fallback.sources[name] for name in values if name in fallback.sources},
    )


def _grobid_reason(metadata: BibliographicMetadata) -> str:
    missing: list[str] = []
    if metadata.title is None:
        missing.append("title")
    if not metadata.authors:
        missing.append("authors")
    if metadata.doi is None and metadata.arxiv_id is None:
        missing.append("identifier")
    return "missing=" + ",".join(missing)


def _existing_metadata(paper: PaperRecord) -> BibliographicMetadata:
    def user_value(name: str, value: object) -> object | None:
        source = paper.metadata_sources.get(name)
        return value if value not in (None, (), "") and source in (None, "user") else None

    title = user_value("title", paper.title)
    abstract = user_value("abstract", paper.abstract)
    authors = user_value("authors", paper.authors)
    year = user_value("year", paper.year)
    venue = user_value("venue", paper.venue)
    doi = user_value("doi", paper.doi)
    arxiv_id = user_value("arxiv_id", paper.arxiv_id)
    source_url = user_value("source_url", paper.source_url)
    values = {
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source_url": source_url,
    }
    sources = {name: "user" for name, value in values.items() if value is not None}
    return BibliographicMetadata(
        title=str(title) if title is not None else None,
        abstract=str(abstract) if abstract is not None else None,
        authors=tuple(str(author) for author in authors) if isinstance(authors, tuple) else (),
        year=int(year) if isinstance(year, int) else None,
        venue=str(venue) if venue is not None else None,
        doi=str(doi) if doi is not None else None,
        arxiv_id=str(arxiv_id) if arxiv_id is not None else None,
        source_url=str(source_url) if source_url is not None else None,
        sources=sources,
    )


def _titles_match(expected: str | None, actual: str | None) -> bool:
    if expected is None or actual is None:
        return True
    expected_words = set(re.findall(r"[a-z0-9]+", expected.lower()))
    actual_words = set(re.findall(r"[a-z0-9]+", actual.lower()))
    if not expected_words or not actual_words:
        return True
    return len(expected_words & actual_words) / min(len(expected_words), len(actual_words)) >= 0.6
