from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from passagen.config import ProvidersSettings
from passagen.external.availability import grobid_status, http_status, llm_status


class ProviderUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    name: str
    available: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ProviderHealthSnapshot:
    statuses: dict[str, ProviderStatus]

    def require(self, name: str) -> None:
        status = self.statuses[name]
        if not status.available:
            raise ProviderUnavailableError(f"Provider {name} is unavailable: {status.detail}")


def llm_health_key(profile_name: str) -> str:
    return "llm" if profile_name == "default" else f"llm:{profile_name}"


def check_provider_health(settings: ProvidersSettings) -> ProviderHealthSnapshot:
    timeout = settings.healthcheck_timeout_seconds
    default_llm = settings.llm.default
    checks = {
        "grobid": lambda: grobid_status(settings.grobid.base_url, timeout),
        "llm": lambda: llm_status(default_llm.base_url, default_llm.api_key_env, timeout),
    }
    for profile_name, profile in settings.llm.profiles.items():
        checks[f"llm:{profile_name}"] = partial(
            llm_status, profile.base_url, profile.api_key_env, timeout
        )
    statuses: dict[str, ProviderStatus] = {}
    if settings.crossref.enabled:
        checks["crossref"] = lambda: http_status(
            settings.crossref.base_url.rstrip("/") + "/works",
            timeout,
            params={"rows": "0"},
        )
    else:
        statuses["crossref"] = ProviderStatus("crossref", False, "disabled by configuration")
    if settings.arxiv.enabled:
        checks["arxiv"] = lambda: http_status(
            settings.arxiv.base_url.rstrip("/") + "/api/query",
            timeout,
            params={"search_query": "all:test", "max_results": "1"},
        )
    else:
        statuses["arxiv"] = ProviderStatus("arxiv", False, "disabled by configuration")
    if settings.openalex.enabled:
        openalex_params = {"search": "test", "per-page": "1"}
        if settings.openalex.mailto:
            openalex_params["mailto"] = settings.openalex.mailto
        checks["openalex"] = lambda: http_status(
            settings.openalex.base_url.rstrip("/") + "/works",
            timeout,
            params=openalex_params,
        )
    else:
        statuses["openalex"] = ProviderStatus("openalex", False, "disabled by configuration")
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = {name: executor.submit(check) for name, check in checks.items()}
        for name, future in futures.items():
            available, detail = future.result()
            statuses[name] = ProviderStatus(name, available, detail)
    return ProviderHealthSnapshot(statuses)


def check_parser_health(settings: ProvidersSettings) -> ProviderHealthSnapshot:
    available, detail = grobid_status(
        settings.grobid.base_url,
        settings.healthcheck_timeout_seconds,
    )
    return ProviderHealthSnapshot({"grobid": ProviderStatus("grobid", available, detail)})
