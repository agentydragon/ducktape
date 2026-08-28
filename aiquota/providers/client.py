import json
import sys
from typing import Protocol

import httpx


class ProviderClientFactory(Protocol):
    """Builds an HTTP client that captures the bodies of `response_urls`.

    The first argument names the capture slot the bodies land in. It is the
    provider name for a provider's own quota endpoint, and
    `models.history_capture_key(...)` for each further endpoint it reads.
    """

    def __call__(self, capture_key: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient: ...


def provider_client(debug: bool = False, transport: httpx.AsyncBaseTransport | None = None) -> ProviderClientFactory:
    def create(capture_key: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient:
        async def dump_response(response: httpx.Response) -> None:
            if str(response.request.url) not in response_urls:
                return
            await response.aread()
            print(
                f"--- {capture_key} response: {response.request.method} {response.request.url} -> {response.status_code} ---",
                file=sys.stderr,
            )
            try:
                body = json.dumps(response.json(), indent=2)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = response.text
            print(body, file=sys.stderr)

        return httpx.AsyncClient(
            timeout=timeout, transport=transport, event_hooks={"response": [dump_response]} if debug else None
        )

    return create
