"""Notification source: canonical Action events, through the ordinary read APIs."""

import asyncio
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from agentplane.action_service.models import ActionEventView, ActionRequestView
from agentplane.subjects import ServiceAccountRef

_EVENTS = TypeAdapter(list[ActionEventView])


class SourceNotOwnedError(Exception):
    pass


class Actions:
    def __init__(self, http: httpx.AsyncClient, token_file: Path) -> None:
        self.http = http
        self.token_file = token_file

    async def events(self, owner: ServiceAccountRef, request_id: UUID, after_sequence: int) -> list[ActionEventView]:
        token = (await asyncio.to_thread(self.token_file.read_text)).strip()
        headers = {"Authorization": f"Bearer {token}"}
        # Broad worker read authority must not become cross-account subscription authority.
        request = await self.http.get(f"/v1/action-requests/{request_id}", headers=headers)
        request.raise_for_status()
        if ActionRequestView.model_validate_json(request.content).caller != owner:
            raise SourceNotOwnedError
        response = await self.http.get(
            f"/v1/action-requests/{request_id}/events",
            params={"after_sequence": after_sequence, "limit": 128},
            headers=headers,
        )
        response.raise_for_status()
        return _EVENTS.validate_json(response.content)
