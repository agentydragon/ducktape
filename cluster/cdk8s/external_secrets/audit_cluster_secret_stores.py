"""Read-only audit of stored ClusterSecretStores. This file runs with stdlib only."""

from __future__ import annotations

import json
import ssl
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_CREDENTIALS = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_STORE_API = "https://kubernetes.default.svc/apis/external-secrets.io/v1/clustersecretstores"


def audit(fetch_page: Callable[[str], dict[str, Any]]) -> int:
    invalid = []
    total = 0
    continuation = ""
    try:
        while True:
            page = fetch_page(continuation)
            for store in page["items"]:
                total += 1
                conditions = store["spec"].get("conditions") or []
                if not any(condition and condition.get("namespaces") for condition in conditions):
                    invalid.append(store["metadata"]["name"])
            continuation = page["metadata"].get("continue", "")
            if not continuation:
                break
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ClusterSecretStore audit could not complete ({type(error).__name__}).", file=sys.stderr)
        return 2
    for name in sorted(invalid):
        print(f"ClusterSecretStore {name} has no explicit consumer namespace.", file=sys.stderr)
    print(f"Checked {total} ClusterSecretStores. Violations: {len(invalid)}.")
    return int(bool(invalid))


def _fetch_page(continuation: str) -> dict[str, Any]:
    token = (_CREDENTIALS / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(_CREDENTIALS / "ca.crt"))
    query = urlencode({"limit": 25, "continue": continuation})
    request = Request(f"{_STORE_API}?{query}", headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, context=context, timeout=10) as response:
        page: dict[str, Any] = json.load(response)
        return page


if __name__ == "__main__":
    raise SystemExit(audit(_fetch_page))
