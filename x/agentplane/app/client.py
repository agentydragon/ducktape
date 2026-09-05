"""A Python client for the integration app's HTTP surface, beside the runner's own client.

The app's request and response models are what it speaks: `NewSandbox`, `EgressGrant` and
`NewSession` go out, `SandboxView`, `BindingView` and `Decision` come back, and a session's events
arrive as the runner protocol's own `Event` messages rather than as dictionaries to pick apart. A
caller dispatches on `WhichOneof("observation")`, the same way the app and the runner do.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from x.agentplane.app.api import EgressGrant, ModelCatalog, Provider
from x.agentplane.app.bridge import NewSession
from x.agentplane.app.decisions import Decision
from x.agentplane.app.egress import BindingView, PolicyView
from x.agentplane.app.inventory import NewSandbox, ProvisioningState, SandboxView
from x.agentplane.app.presets import SandboxPresetView
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

REQUEST_SECONDS = 30.0

PROTO_PROVIDERS = {Provider.CLAUDE: pb.PROVIDER_CLAUDE, Provider.CODEX: pb.PROVIDER_CODEX}


class SessionStreamError(Exception):
    """The app ended a session's event stream with an error frame."""


@dataclass(frozen=True)
class Attachment:
    """What opening a session answers: the session's spec, and where its log stands."""

    attached: pb.Attached

    @property
    def last_sequence(self) -> int:
        return self.attached.last_sequence


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
        """The decoded body, or None when there is none: the app answers 202 to an accepted input
        and 204 to a lifecycle command, both with an empty body, so the status alone does not say
        whether there is anything to decode."""
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def models(self) -> ModelCatalog:
        """Which models each harness may be opened with, as this deployment is configured."""
        return {Provider(harness): names for harness, names in (await self._json("GET", "/models")).items()}

    async def presets(self) -> list[SandboxPresetView]:
        return [SandboxPresetView.model_validate(row) for row in await self._json("GET", "/presets")]

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

    async def open_session(self, name: str, session_id: str, spec: pb.SessionSpec) -> Attachment:
        body = NewSession(session_id=session_id, spec=MessageToDict(spec))
        answered = await self._json("POST", f"/sandboxes/{name}/sessions", json=body.model_dump())
        return Attachment(ParseDict(answered, pb.Attached()))

    async def open_preset_session(
        self, name: str, session_id: str, *, overrides: dict[str, object] | None = None, preset: str | None = None
    ) -> Attachment:
        """Open from a Sandbox binding or explicit ThreadPreset, sending only caller overrides."""
        body = NewSession(session_id=session_id, spec=overrides or {}, preset=preset)
        answered = await self._json("POST", f"/sandboxes/{name}/sessions", json=body.model_dump())
        return Attachment(ParseDict(answered, pb.Attached()))

    async def send_input(self, name: str, session_id: str, message: pb.Input) -> None:
        await self._json("POST", f"/sandboxes/{name}/sessions/{session_id}/inputs", json=MessageToDict(message))

    async def events(self, name: str, session_id: str, *, after: int, read_seconds: float) -> AsyncIterator[pb.Event]:
        """The session's events from `after`. Ends when the stream does; `read_seconds` bounds how
        long a single frame may take to arrive, so a wedged session fails a caller rather than
        hanging it.

        Server-sent events, parsed as the format actually specifies: a frame is terminated by a
        blank line, `data:` may repeat and is joined with newlines, and a comment line (`:`) is a
        keepalive. A frame carrying no data resets the parser like any other, so a name never
        leaks into the frame after it.
        """
        timeout = httpx.Timeout(REQUEST_SECONDS, read=read_seconds)
        url = f"/sandboxes/{name}/sessions/{session_id}/events"
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
                        raise SessionStreamError(f"{name}/{session_id}: {payload}")
                    if frame == "event" and payload:
                        yield _event(payload)


def _event(payload: str) -> pb.Event:
    """One `event` frame's data as the protocol's own message; a payload that is not one says so."""
    try:
        return ParseDict(json.loads(payload), pb.Event())
    except (ValueError, ParseError) as error:
        raise SessionStreamError(f"not an Event: {error}: {payload[:400]!r}") from error


def is_running(view: SandboxView) -> bool:
    """Whether the sandbox has a Pod with an address.

    Necessary for a session and not sufficient: the runner in that Pod may not be listening yet, and
    the app answers `503` until it is.
    """
    return view.state is ProvisioningState.RUNNING and view.pod is not None and view.pod.ip is not None
