"""Small client for CLIProxyAPI's authenticated management surface."""

from dataclasses import dataclass

import httpx

from aiquota.providers.client import ProviderClientFactory

MANAGEMENT_AUTH_FILES_PATH = "/auth-files"
MANAGEMENT_API_CALL_PATH = "/api-call"


def _management_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _select_auth_index(payload: object, provider: str) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return None
    candidates = [
        entry
        for entry in payload["files"]
        if isinstance(entry, dict)
        and str(entry.get("provider", "")).lower() == provider.lower()
        and not entry.get("disabled", False)
        and not entry.get("unavailable", False)
        and entry.get("auth_index")
    ]
    if len(candidates) != 1:
        return None
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
    auth_index = _select_auth_index(auth_files_response.json(), provider)
    if not auth_index:
        raise ValueError(f"expected exactly one available {provider.capitalize()} auth file")

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

    async def fetch_usage(self, provider: str, usage_url: str, usage_headers: dict[str, str]) -> str:
        if not self.key:
            raise ValueError("CLIProxyAPI key is not configured")
        api_call_url = f"{self.url.rstrip('/')}{MANAGEMENT_API_CALL_PATH}"
        async with self.client_factory(provider, {api_call_url}, self.timeout) as client:
            return await _fetch_usage_via_management(self.url, self.key, provider, usage_url, usage_headers, client)
