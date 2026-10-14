"""A Python client for the integration app's HTTP surface, beside the runner's own client.

The app's request and response models are what it speaks: `NewSandbox` and
`NewSession` go out, `SandboxView`, `BindingView` and `Decision` come back, and a session's events
arrive as shared `EventEntry` messages rather than as dictionaries to pick apart. A caller
dispatches on `entry.event.WhichOneof("observation")`, the same way the app and the runner do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import httpx
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from agentplane.app.live import DecisionsSnapshot, DecisionsUnavailable, SandboxesSnapshot
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.presets import SandboxPresetView
from agentplane.app.sandbox_models import NewSandbox, SandboxView
from agentplane.app.threads.bridge import NewSession
from agentplane.app.threads.thread_view import ThreadView
from agentplane.egress.decisions import Decision
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.sandboxes.egress_views import BindingView

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

REQUEST_SECONDS = 30.0


# How long a Thread's stream may stay down, reconnecting, before the caller is told. Testing's app
# Deployment recreates its only replica, which took about three minutes to answer again (migrate
# initContainer, then startup) when its pod was deleted on 2026-10-13.
RECONNECT_SECONDS = 600.0
RECONNECT_PAUSE_SECONDS = 2.0
_ROLLING_STATUSES = frozenset({HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.GATEWAY_TIMEOUT})


def _no_app_answered(response: httpx.Response) -> bool:
    """What the public route answers while no app replica is ready: a 502/503/504, and on testing
    intermittently the proxy's own `403` with a plain-text "Access denied" body. The app's own
    refusals are JSON, so a JSON `403` is a verdict, not a gap."""
    return response.status_code in _ROLLING_STATUSES or (
        response.status_code == HTTPStatus.FORBIDDEN
        and response.headers.get("content-type", "").startswith("text/plain")
    )


class SessionStreamError(Exception):
    """The app ended a session's event stream with an error frame, or it stayed down too long."""


class _StreamDroppedError(Exception):
    """The stream's replica went away; the stream is to be reopened after its last entry."""


@dataclass(frozen=True)
class Attachment:
    """What opening a session answers: the session's spec, and where its log stands."""

    attached: runner_pb2.Attached

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
        return ModelCatalog.model_validate(await self._json("GET", "/models"))

    async def presets(self) -> list[SandboxPresetView]:
        return [SandboxPresetView.model_validate(row) for row in await self._json("GET", "/presets")]

    async def create_sandbox(self, spec: NewSandbox) -> SandboxView:
        return SandboxView.model_validate(
            await self._json("POST", "/sandboxes", json=spec.model_dump(exclude_unset=True))
        )

    async def sandbox(self, name: str) -> SandboxView:
        return SandboxView.model_validate(await self._json("GET", f"/sandboxes/{name}"))

    async def sandbox_snapshots(self) -> AsyncGenerator[SandboxesSnapshot]:
        """Initial inventory and watch-triggered changes; the caller bounds the subscription.

        This lifecycle stream fails immediately on HTTP refusals or connection loss. It does not
        use the Thread journal's rollout-reconnect policy.
        """
        async with self._http.stream(
            "GET",
            "/live/sandboxes",
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(REQUEST_SECONDS, read=None),
        ) as response:
            response.raise_for_status()
            kind, data = "message", []
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    kind = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data.append(line.removeprefix("data:").lstrip())
                elif not line:
                    frame, payload = kind, "\n".join(data)
                    kind, data = "message", []
                    if frame == "snapshot" and payload:
                        yield SandboxesSnapshot.model_validate_json(payload)
        raise RuntimeError("the Sandbox inventory stream ended")

    async def suspend_sandbox(self, name: str) -> None:
        await self._json("POST", f"/sandboxes/{name}/suspend")

    async def resume_sandbox(self, name: str) -> None:
        await self._json("POST", f"/sandboxes/{name}/resume")

    async def delete_sandbox(self, name: str) -> None:
        await self._json("DELETE", f"/sandboxes/{name}")

    async def bindings(self, name: str) -> list[BindingView]:
        return [BindingView.model_validate(row) for row in await self._json("GET", f"/sandboxes/{name}/egress")]

    async def decisions(self, name: str) -> list[Decision]:
        """The first frame of the sandbox's egress decision stream, oldest first."""
        async with self._http.stream(
            "GET", f"/live/sandboxes/{name}/egress/decisions", headers={"Accept": "text/event-stream"}
        ) as response:
            response.raise_for_status()
            kind = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    kind = line.removeprefix("event:").strip()
                elif line.startswith("data:") and kind == "snapshot":
                    snapshot = DecisionsSnapshot.model_validate_json(line.removeprefix("data:"))
                    if isinstance(snapshot.decisions, DecisionsUnavailable):
                        raise RuntimeError(f"egress decisions of {name} unavailable: {snapshot.decisions.detail}")
                    return snapshot.decisions
        raise RuntimeError(f"the egress decision stream of {name} ended without a snapshot")

    async def create_session(
        self,
        name: str,
        session_id: UUID,
        *,
        overrides: dict[str, object] | None = None,
        setup_script: str | None = None,
    ) -> Attachment:
        """Create a Session from the defaults recorded on the Sandbox plus proto-JSON overrides.

        Retrying with the same Session ID and overrides answers the same Session.
        """
        body = NewSession(session_id=session_id, spec=overrides or {}, setup_script=setup_script)
        answered = await self._json("POST", f"/sandboxes/{name}/sessions", json=body.model_dump(mode="json"))
        return Attachment(ParseDict(answered, runner_pb2.Attached()))

    async def resume_thread(self, thread_id: UUID) -> Attachment:
        """Resume the runner session already bound to this Thread."""
        answered = await self._json("POST", f"/threads/{thread_id}/resume")
        return Attachment(ParseDict(answered, runner_pb2.Attached()))

    async def thread(self, thread_id: UUID) -> ThreadView:
        """The persistent Thread of a created Session; its ID is the Session's public ID."""
        return ThreadView.model_validate(await self._json("GET", f"/threads/{thread_id}"))

    async def command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Return the archived runner CommandAdmitted entry, not a native command outcome."""
        answered = await self._json("POST", f"/threads/{thread_id}/commands", json=MessageToDict(command))
        return ParseDict(answered, event_log_pb2.EventEntry())

    async def events(
        self, thread_id: UUID, *, after: int, read_seconds: float
    ) -> AsyncIterator[event_log_pb2.EventEntry]:
        """The Thread's events from `after`, ending when the app sends the `end` frame.

        A stream that drops without one -- an app rollout or crash, a closed connection, a `502`, `503` or `504`, or the proxy's plain-text `403`, while no replica is up -- is reopened after the last entry yielded, so a
        caller sees every entry once and in order. An outage that outlasts `RECONNECT_SECONDS`
        without delivering an entry raises. `read_seconds` bounds how long a single frame may take
        to arrive, so a wedged session fails a caller rather than hanging it.

        Server-sent events, parsed as the format actually specifies: a frame is terminated by a
        blank line, `data:` may repeat and is joined with newlines, and a comment line (`:`) is a
        keepalive. A frame carrying no data resets the parser like any other, so a name never
        leaks into the frame after it.
        """
        timeout = httpx.Timeout(REQUEST_SECONDS, read=read_seconds)
        url = f"/threads/{thread_id}/events/stream"
        cursor = after
        outage_began: float | None = None
        while True:
            try:
                async with self._http.stream(
                    "GET", url, params={"after": cursor}, headers={"Accept": "text/event-stream"}, timeout=timeout
                ) as response:
                    if _no_app_answered(response):
                        raise _StreamDroppedError(f"{response.status_code=}")
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
                                entry = _event(payload)
                                cursor = entry.cursor
                                outage_began = None
                                yield entry
                    reason = "the stream closed without an end frame"
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError) as error:
                reason = f"{error!r}"
            except _StreamDroppedError as error:
                reason = str(error)
            outage_began = outage_began if outage_began is not None else time.monotonic()
            if time.monotonic() - outage_began > RECONNECT_SECONDS:
                raise SessionStreamError(
                    f"Thread {thread_id}: no event for {RECONNECT_SECONDS}s after {cursor=}: {reason}"
                )
            logger.warning("Thread %s stream dropped (%s); resuming after cursor %d", thread_id, reason, cursor)
            await asyncio.sleep(RECONNECT_PAUSE_SECONDS)


def _event(payload: str) -> event_log_pb2.EventEntry:
    """One `event` frame's data as an EventEntry; a payload that is not one says so."""
    try:
        return ParseDict(json.loads(payload), event_log_pb2.EventEntry())
    except (ValueError, ParseError) as error:
        raise SessionStreamError(f"not an EventEntry: {error}: {payload[:400]!r}") from error
