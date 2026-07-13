import json
import sys

import httpx


def dump_response(provider: str, response: httpx.Response) -> None:
    print(
        f"--- {provider} response: {response.request.method} {response.request.url} -> {response.status_code} ---",
        file=sys.stderr,
    )
    try:
        body = json.dumps(response.json(), indent=2)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = response.text
    print(body, file=sys.stderr)
