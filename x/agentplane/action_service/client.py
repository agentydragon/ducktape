"""Small, separate clients for Sandbox workload and operator/BFF API surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from x.agentplane.action_service.models import ActionEventView, ActionRequestInput, ActionRequestView, DecisionInput

WORKLOAD_CREDENTIAL_PLACEHOLDER = "agentplane-credential-agentplane-workload"


class AccessTokenProvider(Protocol):
    async def token(self) -> str: ...


@dataclass(frozen=True)
class CredentialPlaceholder:
    """Non-secret runner input selected for central authenticatedWorkloadToken substitution."""

    value: str = WORKLOAD_CREDENTIAL_PLACEHOLDER

    async def token(self) -> str:
        return self.value


class _BearerClient:
    def __init__(self, http: httpx.AsyncClient, tokens: AccessTokenProvider) -> None:
        self._http = http
        self._tokens = tokens

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = await self._tokens.token()
        response = await self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        response.raise_for_status()
        return response


class ActionServiceClient(_BearerClient):
    """Sandbox-facing client; normally presents only the public central-proxy placeholder."""

    async def submit(self, body: ActionRequestInput) -> ActionRequestView:
        response = await self._request("POST", "/v1/action-requests", json=body.model_dump(mode="json"))
        return ActionRequestView.model_validate(response.json())

    async def get(self, request_id: UUID) -> ActionRequestView:
        response = await self._request("GET", f"/v1/action-requests/{request_id}")
        return ActionRequestView.model_validate(response.json())

    async def events(self, request_id: UUID, *, after_sequence: int = 0) -> list[ActionEventView]:
        """The durable, ordered transition log; `after_sequence` makes repeated polling a no-op."""
        response = await self._request(
            "GET", f"/v1/action-requests/{request_id}/events", params={"after_sequence": after_sequence}
        )
        return [ActionEventView.model_validate(event) for event in response.json()]


class OperatorActionServiceClient(_BearerClient):
    """BFF-facing client; its authenticator and paths are distinct from Sandbox workload auth."""

    async def get(self, request_id: UUID) -> ActionRequestView:
        response = await self._request("GET", f"/v1/operator/action-requests/{request_id}")
        return ActionRequestView.model_validate(response.json())

    async def decide(self, request_id: UUID, body: DecisionInput) -> ActionRequestView:
        response = await self._request(
            "POST", f"/v1/operator/action-requests/{request_id}/decision", json=body.model_dump(mode="json")
        )
        return ActionRequestView.model_validate(response.json())
