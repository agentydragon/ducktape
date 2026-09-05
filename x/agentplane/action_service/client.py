"""Small client boundary for BFFs, external harnesses, and managed-sandbox relays."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx

from x.agentplane.action_service.models import ActionRequestInput, ActionRequestView, DecisionInput


class AccessTokenProvider(Protocol):
    async def token(self) -> str: ...


class ProjectedTokenFile:
    """Relay-only token source: re-read kubelet's short-lived projection for every service call."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def token(self) -> str:
        return self._path.read_text().strip()


class ActionServiceClient:
    def __init__(self, http: httpx.AsyncClient, tokens: AccessTokenProvider) -> None:
        self._http = http
        self._tokens = tokens

    async def submit(self, body: ActionRequestInput) -> ActionRequestView:
        response = await self._request("POST", "/v1/action-requests", json=body.model_dump(mode="json"))
        return ActionRequestView.model_validate(response.json())

    async def get(self, request_id: UUID) -> ActionRequestView:
        response = await self._request("GET", f"/v1/action-requests/{request_id}")
        return ActionRequestView.model_validate(response.json())

    async def decide(self, request_id: UUID, body: DecisionInput) -> ActionRequestView:
        response = await self._request(
            "POST", f"/v1/action-requests/{request_id}/decision", json=body.model_dump(mode="json")
        )
        return ActionRequestView.model_validate(response.json())

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = await self._tokens.token()
        response = await self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        response.raise_for_status()
        return response
