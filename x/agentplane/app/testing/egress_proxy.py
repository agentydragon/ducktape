"""A stand-in for the egress proxy's admin port: canned decisions, or a proxy nobody can reach."""

from __future__ import annotations

from typing import Any

import httpx


class FakeEgressAdmin:
    def __init__(self) -> None:
        self.decisions: dict[str, list[dict[str, Any]]] = {}
        self.reachable = True
        self.queries: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("connection refused", request=request)
        assert request.url.path == "/decisions"
        sandbox = request.url.params["sandbox"]
        self.queries.append(sandbox)
        return httpx.Response(200, json=self.decisions.get(sandbox, []))


def decision(
    at: str,
    method: str,
    host: str,
    path: str | None,
    outcome: str,
    *,
    reason: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """One decision as the proxy's `/decisions` serialises it."""
    return {
        "at": at,
        "sandbox": "live",
        "method": method,
        "host": host,
        "port": 443,
        "path": path,
        "outcome": outcome,
        "reason": reason,
        "binding": "sandboxes-github-public" if outcome == "allow" else None,
        "granted_by": "flux" if outcome == "allow" else None,
        "policy": "github-public" if outcome == "allow" else None,
        "rule": 0 if outcome == "allow" else None,
        "substituted": outcome == "allow" and method != "CONNECT",
        "address": address if outcome == "allow" else None,
    }
