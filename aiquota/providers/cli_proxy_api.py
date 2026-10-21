"""Small client for CLIProxyAPI's authenticated management surface."""

from dataclasses import dataclass

import httpx

from aiquota.providers.client import ProviderClientFactory

MANAGEMENT_AUTH_FILES_PATH = "/auth-files"
MANAGEMENT_API_CALL_PATH = "/api-call"


def _management_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _select_auth_index(payload: object, provider: str) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("invalid CLIProxyAPI /auth-files response")
    candidates = [
        entry
        for entry in payload["files"]
        if isinstance(entry, dict)
        and str(entry.get("provider", "")).lower() == provider.lower()
        and not entry.get("disabled", False)
        and not entry.get("unavailable", False)
        and entry.get("auth_index")
    ]
    if not candidates:
        raise ValueError(f"no available {provider.capitalize()} auth file found (0 eligible matches)")
    if len(candidates) > 1:
        raise ValueError(
            f"multiple available {provider.capitalize()} auth files found ({len(candidates)} eligible matches)"
        )
    return str(candidates[0]["auth_index"])


async def _fetch_usage_via_management(
    cli_proxy_api_url: str,
    cli_proxy_api_key: str,
    provider: str,
    usage_url: str,
    usage_headers: dict[str, str],
    client: httpx.AsyncClient,
) -> str:
    """Ask CLIProxyAPI to call a provider usage endpoint with its auth file.

    The auth index is an opaque runtime selector, not credential state. The
    provider never receives or persists the selected auth file or token.
    """

    base_url = cli_proxy_api_url.rstrip("/")
    auth_files_response = await client.get(
        f"{base_url}{MANAGEMENT_AUTH_FILES_PATH}", headers=_management_headers(cli_proxy_api_key)
    )
    auth_files_response.raise_for_status()
    try:
        auth_files_payload = auth_files_response.json()
    except ValueError as error:
        raise ValueError("invalid CLIProxyAPI /auth-files response") from error
    auth_index = _select_auth_index(auth_files_payload, provider)

    api_call_response = await client.post(
        f"{base_url}{MANAGEMENT_API_CALL_PATH}",
        headers={**_management_headers(cli_proxy_api_key), "Content-Type": "application/json"},
        json={"auth_index": auth_index, "method": "GET", "url": usage_url, "header": usage_headers},
    )
    api_call_response.raise_for_status()
    envelope = api_call_response.json()
    if not isinstance(envelope, dict) or not isinstance(envelope.get("status_code"), int):
        raise ValueError("invalid CLIProxyAPI /api-call response")
    status_code = envelope["status_code"]
    body = envelope.get("body", "")
    if not isinstance(body, str):
        raise ValueError("invalid CLIProxyAPI /api-call body")
    if status_code >= 400:
        upstream_response = httpx.Response(status_code, content=body, request=api_call_response.request)
        upstream_response.raise_for_status()
    return body


@dataclass(frozen=True)
class CLIProxyAPIManagementClient:
    """Client for provider usage calls through CLIProxyAPI management."""

    url: str
    key: str | None
    client_factory: ProviderClientFactory
    timeout: float = 5.0

    async def fetch_usage(
        self,
        provider: str,
        usage_url: str,
        usage_headers: dict[str, str],
        capture_key: str,
        *,
        capture_response: bool = True,
    ) -> str:
        """Call one provider endpoint through CLIProxyAPI.

        `provider` selects the auth file; `capture_key` names the raw-response
        slot, so a provider reading several endpoints keeps each body. Set
        `capture_response` false for lookup endpoints whose body is only used to
        discover the URL of another endpoint and should not be retained.
        """

        if not self.key:
            raise ValueError("CLIProxyAPI key is not configured")
        api_call_url = f"{self.url.rstrip('/')}{MANAGEMENT_API_CALL_PATH}"
        response_urls = {api_call_url} if capture_response else set()
        async with self.client_factory(capture_key, response_urls, self.timeout) as client:
            return await _fetch_usage_via_management(self.url, self.key, provider, usage_url, usage_headers, client)
