import os

import httpx


def http_status(
    url: str,
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        response = httpx.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, str(exc)
    return response.status_code < 500, f"HTTP {response.status_code}"


def grobid_status(base_url: str, timeout: float) -> tuple[bool, str]:
    try:
        response = httpx.get(base_url.rstrip("/") + "/api/isalive", timeout=timeout)
    except httpx.HTTPError as exc:
        return False, str(exc)
    available = response.is_success and response.text.strip().lower() == "true"
    return available, f"HTTP {response.status_code} body={response.text.strip()!r}"


def llm_status(
    base_url: str,
    api_key_env: str,
    timeout: float,
) -> tuple[bool, str]:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return False, f"environment variable {api_key_env} is not set"
    return http_status(
        base_url.rstrip("/") + "/models",
        timeout,
        headers={"Authorization": f"Bearer {api_key}"},
    )
