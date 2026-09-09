"""Small, separate clients for Sandbox workload and operator/BFF API surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from x.agentplane.action_service.connections import Connection, ConnectionRename, ConnectionVersion, Identity
from x.agentplane.action_service.enrollments import (
    EnrollmentDecisionInput,
    EnrollmentDecisionResult,
    EnrollmentPreview,
    EnrollmentPreviewInput,
)
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CancellationResult,
    DecisionInput,
)

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

    async def cancel(self, request_id: UUID) -> CancellationResult:
        response = await self._request("POST", f"/v1/action-requests/{request_id}/cancel")
        return CancellationResult.model_validate(response.json())

    async def events(self, request_id: UUID, *, after_sequence: int = 0) -> list[ActionEventView]:
        """The durable, ordered transition log; `after_sequence` makes repeated polling a no-op."""
        response = await self._request(
            "GET", f"/v1/action-requests/{request_id}/events", params={"after_sequence": after_sequence}
        )
        return [ActionEventView.model_validate(event) for event in response.json()]


class OperatorActionServiceClient(_BearerClient):
    """BFF-facing client; its authenticator and paths are distinct from Sandbox workload auth."""

    async def list_identities(self) -> dict[str, Identity]:
        response = await self._request("GET", "/v1/operator/identities")
        return {key: Identity.model_validate(value) for key, value in response.json().items()}

    async def preview_enrollment(self, handle: str, body: EnrollmentPreviewInput) -> EnrollmentPreview:
        response = await self._request(
            "POST", f"/v1/operator/connection-enrollments/{handle}/preview", json=body.model_dump(mode="json")
        )
        return EnrollmentPreview.model_validate(response.json())

    async def decide_enrollment(self, handle: str, body: EnrollmentDecisionInput) -> EnrollmentDecisionResult:
        response = await self._request(
            "POST", f"/v1/operator/connection-enrollments/{handle}/decision", json=body.model_dump(mode="json")
        )
        return EnrollmentDecisionResult.model_validate(response.json())

    async def connections(self) -> list[Connection]:
        response = await self._request("GET", "/v1/operator/connections")
        return [Connection.model_validate(row) for row in response.json()]

    async def connection(self, connection_id: UUID) -> Connection:
        response = await self._request("GET", f"/v1/operator/connections/{connection_id}")
        return Connection.model_validate(response.json())

    async def rename_connection(self, connection_id: UUID, body: ConnectionRename) -> Connection:
        response = await self._request(
            "PATCH", f"/v1/operator/connections/{connection_id}", json=body.model_dump(mode="json")
        )
        return Connection.model_validate(response.json())

    async def unbind_connection(self, connection_id: UUID, body: ConnectionVersion) -> Connection:
        response = await self._request(
            "POST", f"/v1/operator/connections/{connection_id}/unbind", json=body.model_dump(mode="json")
        )
        return Connection.model_validate(response.json())

    async def list_requests(self, *, states: tuple[ActionState, ...] = ()) -> list[ActionRequestView]:
        response = await self._request(
            "GET", "/v1/operator/action-requests", params=[("state", state) for state in states]
        )
        return [ActionRequestView.model_validate(row) for row in response.json()]

    async def get(self, request_id: UUID) -> ActionRequestView:
        response = await self._request("GET", f"/v1/operator/action-requests/{request_id}")
        return ActionRequestView.model_validate(response.json())

    async def events(self, request_id: UUID, *, after_sequence: int = 0) -> list[ActionEventView]:
        response = await self._request(
            "GET", f"/v1/operator/action-requests/{request_id}/events", params={"after_sequence": after_sequence}
        )
        return [ActionEventView.model_validate(event) for event in response.json()]

    async def decide(self, request_id: UUID, body: DecisionInput) -> ActionRequestView:
        response = await self._request(
            "POST", f"/v1/operator/action-requests/{request_id}/decision", json=body.model_dump(mode="json")
        )
        return ActionRequestView.model_validate(response.json())
