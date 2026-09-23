"""A Python client for the integration app's HTTP surface, beside the runner's own client.

The app's request and response models are what it speaks: `NewSandbox`, `EgressGrant` and
`NewSession` go out, `SandboxView`, `BindingView` and `Decision` come back, and a session's events
arrive as shared `EventEntry` messages rather than as dictionaries to pick apart. A caller
dispatches on `entry.event.WhichOneof("observation")`, the same way the app and the runner do.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import httpx
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from agentplane.app.api import EgressGrant, ModelCatalog
from agentplane.app.bridge import NewSession
from agentplane.app.decisions import Decision
from agentplane.app.egress import BindingView, PolicyView
from agentplane.app.inventory import NewSandbox, ProvisioningState, SandboxView
from agentplane.app.presets import Harness, SandboxPresetView
from agentplane.app.thread.views import ThreadView
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

REQUEST_SECONDS = 30.0


class SessionStreamError(Exception):
    """The app ended a session's event stream with an error frame."""


@dataclass(frozen=True)
class Attachment:
    """What opening a session answers: the session's spec, and where its log stands."""

    attached: protocol_pb2.Attached

    @property
    def last_cursor(self) -> int:
        return self.attached.last_cursor


class Client:
    """Async context manager over one deployment of the app."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(REQUEST_SECONDS),
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        """The decoded body, or None for a successful empty lifecycle response."""
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def models(self) -> ModelCatalog:
        """Which models each harness may be opened with, as this deployment is configured."""
        return {Harness(harness): names for harness, names in (await self._json("GET", "/models")).items()}

    async def presets(self) -> list[SandboxPresetView]:
        return [SandboxPresetView.model_validate(row) for row in await self._json("GET", "/presets")]

    async def templates(self) -> list[str]:
        return list(await self._json("GET", "/sandboxes/templates"))

    async def policies(self) -> list[PolicyView]:
        return [PolicyView.model_validate(row) for row in await self._json("GET", "/egress/policies")]

    async def create_sandbox(self, spec: NewSandbox) -> SandboxView:
        return SandboxView.model_validate(
            await self._json("POST", "/sandboxes", json=spec.model_dump(exclude_unset=True))
        )

    async def sandbox(self, name: str) -> SandboxView:
        return SandboxView.model_validate(await self._json("GET", f"/sandboxes/{name}"))

    async def suspend_sandbox(self, name: str) -> None:
        await self._json("POST", f"/sandboxes/{name}/suspend")

    async def delete_sandbox(self, name: str) -> None:
        await self._json("DELETE", f"/sandboxes/{name}")

    async def grant_egress(self, name: str, policies: list[str]) -> BindingView:
        body = EgressGrant(policies=policies)
        return BindingView.model_validate(await self._json("POST", f"/sandboxes/{name}/egress", json=body.model_dump()))

    async def bindings(self, name: str) -> list[BindingView]:
        return [BindingView.model_validate(row) for row in await self._json("GET", f"/sandboxes/{name}/egress")]

    async def decisions(self, name: str) -> list[Decision]:
        return [Decision.model_validate(row) for row in await self._json("GET", f"/sandboxes/{name}/egress/decisions")]

    async def open_session(self, name: str, session_id: str, spec: protocol_pb2.SessionSpec) -> Attachment:
        body = NewSession(session_id=session_id, spec=MessageToDict(spec))
        answered = await self._json("POST", f"/sandboxes/{name}/sessions", json=body.model_dump())
        return Attachment(ParseDict(answered, protocol_pb2.Attached()))

    async def open_bound_session(
        self, name: str, session_id: str, *, overrides: dict[str, object] | None = None
    ) -> Attachment:
        """Open with the concrete defaults recorded on the Sandbox, plus caller overrides."""
        body = NewSession(session_id=session_id, spec=overrides or {})
        answered = await self._json("POST", f"/sandboxes/{name}/sessions", json=body.model_dump())
        return Attachment(ParseDict(answered, protocol_pb2.Attached()))

    async def thread(self, sandbox: str, session_id: str) -> ThreadView:
        """Resolve the persistent Thread created by a manual runner-session open."""
        threads = await self._json("GET", "/threads", params={"sandbox": sandbox, "session_id": session_id})
        if not threads:
            raise RuntimeError(f"no persisted Thread for {sandbox}/{session_id}")
        return ThreadView.model_validate(threads[0])

    async def command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Return the archived runner CommandAdmitted entry, not a native command outcome."""
        answered = await self._json("POST", f"/threads/{thread_id}/commands", json=MessageToDict(command))
        return ParseDict(answered, event_log_pb2.EventEntry())

    async def events(
        self, thread_id: UUID, *, after: int, read_seconds: float
    ) -> AsyncIterator[event_log_pb2.EventEntry]:
        """The Thread's events from `after`. Ends when the stream does; `read_seconds` bounds how
        long a single frame may take to arrive, so a wedged session fails a caller rather than
        hanging it.

        Server-sent events, parsed as the format actually specifies: a frame is terminated by a
        blank line, `data:` may repeat and is joined with newlines, and a comment line (`:`) is a
        keepalive. A frame carrying no data resets the parser like any other, so a name never
        leaks into the frame after it.
        """
        timeout = httpx.Timeout(REQUEST_SECONDS, read=read_seconds)
        url = f"/threads/{thread_id}/events/stream"
        async with self._http.stream(
            "GET", url, params={"after": after}, headers={"Accept": "text/event-stream"}, timeout=timeout
        ) as response:
            response.raise_for_status()
            kind, data = "message", []
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    kind = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data.append(line.removeprefix("data:").strip())
                elif not line:
                    frame, payload = kind, "\n".join(data)
                    kind, data = "message", []
                    if frame == "end":
                        return
                    if frame == "error":
                        raise SessionStreamError(f"Thread {thread_id}: {payload}")
                    if frame == "event" and payload:
                        yield _event(payload)


def _event(payload: str) -> event_log_pb2.EventEntry:
    """One `event` frame's data as an EventEntry; a payload that is not one says so."""
    try:
        return ParseDict(json.loads(payload), event_log_pb2.EventEntry())
    except (ValueError, ParseError) as error:
        raise SessionStreamError(f"not an EventEntry: {error}: {payload[:400]!r}") from error


def is_running(view: SandboxView) -> bool:
    """Whether the sandbox has a Pod with an address.

    Necessary for a session and not sufficient: the runner in that Pod may not be listening yet, and
    the app answers `503` until it is.
    """
    return view.state is ProvisioningState.RUNNING and view.pod is not None and view.pod.ip is not None
