import json
import sys
from typing import Protocol

import httpx


class ProviderClientFactory(Protocol):
    def __call__(self, provider: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient: ...


def provider_client(
    debug: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderClientFactory:
    def create(provider: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient:
        async def dump_response(response: httpx.Response) -> None:
            if str(response.request.url) not in response_urls:
                return
            await response.aread()
            print(
                f"--- {provider} response: {response.request.method} {response.request.url} -> {response.status_code} ---",
                file=sys.stderr,
            )
            try:
                body = json.dumps(response.json(), indent=2)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = response.text
            print(body, file=sys.stderr)

        return httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            event_hooks={"response": [dump_response]} if debug else None,
        )

    return create
