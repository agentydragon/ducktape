"""A client for the Go integration app's HTTP surface, beside the runner's own client.

Its Pydantic models describe the HTTP contracts used by this client and system tests. A session's
events arrive as shared `EventEntry` messages rather than dictionaries to pick apart; callers
dispatch on `entry.event.WhichOneof("observation")`.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from http import HTTPStatus
from types import TracebackType
from typing import Any, Concatenate, Self
from uuid import UUID

import httpx
from google.protobuf.json_format import ParseDict, ParseError
from opentelemetry import trace

from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.testing.models import (
    BindingView,
    DecisionsSnapshot,
    DecisionsUnavailable,
    NewSandbox,
    NewSession,
    RunnerStatusView,
    SandboxesSnapshot,
    SandboxPresetView,
    SandboxView,
    ThreadPage,
    ThreadView,
)
from agentplane.egress.decisions import Decision
from agentplane.proto import event_log_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

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


def _traced[**P, R](
    method: Callable[Concatenate[Client, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[Client, P], Coroutine[Any, Any, R]]:
    """One `app.<method>` span per call, so a system test's time between its own spans is attributed."""

    @functools.wraps(method)
    async def call(self: Client, *args: P.args, **kwargs: P.kwargs) -> R:
        with tracer.start_as_current_span(f"app.{method.__name__}"):
            return await method(self, *args, **kwargs)

    return call


def _traced_stream[**P, T](
    method: Callable[Concatenate[Client, P], AsyncGenerator[T]],
) -> Callable[Concatenate[Client, P], AsyncGenerator[T]]:
    """One `app.<method>` span from the first read until the stream closes.

    The span is not made current: an async generator resumes in its consumer's context, so a current
    span would be detached from a different context than it was attached in.
    """

    @functools.wraps(method)
    async def stream(self: Client, *args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[T]:
        span = tracer.start_span(f"app.{method.__name__}")
        items = 0
        try:
            async for item in method(self, *args, **kwargs):
                items += 1
                yield item
        finally:
            span.set_attribute("items", items)
            span.end()

    return stream


class SessionStreamError(Exception):
    """The app ended a session's event stream with an error frame, or it stayed down too long."""


class _StreamDroppedError(Exception):
    """The stream's replica went away; the stream is to be reopened after its last entry."""


class Client:
    """Async context manager over one deployment of the app."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self.opened_sandbox_uids: set[UUID] = set()
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
        if response.is_error:
            # The status alone does not say which dependency refused; the problem detail does.
            trace.get_current_span().add_event(
                "http.error", {"status": response.status_code, "body": response.text[:1000]}
            )
        response.raise_for_status()
        return response.json() if response.content else None

    async def models(self) -> ModelCatalog:
        """Which models each harness may be opened with, as this deployment is configured."""
        return ModelCatalog.model_validate(await self._json("GET", "/models"))

    async def presets(self) -> list[SandboxPresetView]:
        return [SandboxPresetView.model_validate(row) for row in await self._json("GET", "/presets")]

    @_traced
    async def create_sandbox(self, spec: NewSandbox) -> SandboxView:
        return SandboxView.model_validate(
            await self._json("POST", "/sandboxes", json=spec.model_dump(exclude_unset=True))
        )

    async def sandbox(self, name: str) -> SandboxView:
        return SandboxView.model_validate(await self._json("GET", f"/sandboxes/{name}"))

    @_traced
    async def runner_status(self, name: str) -> RunnerStatusView:
        return RunnerStatusView.model_validate(await self._json("GET", f"/sandboxes/{name}/runner-status"))

    @_traced_stream
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

    @_traced
    async def suspend_sandbox(self, name: str) -> None:
        await self._json("POST", f"/sandboxes/{name}/suspend")

    @_traced
    async def resume_sandbox(self, name: str) -> None:
        await self._json("POST", f"/sandboxes/{name}/resume")

    @_traced
    async def delete_sandbox(self, name: str) -> None:
        await self._json("DELETE", f"/sandboxes/{name}")

    async def bindings(self, name: str) -> list[BindingView]:
        return [BindingView.model_validate(row) for row in await self._json("GET", f"/sandboxes/{name}/egress")]

    @_traced
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

    @_traced
    async def create_session(
        self,
        name: str,
        session_id: UUID,
        *,
        overrides: dict[str, object] | None = None,
        setup_script: str | None = None,
    ) -> None:
        """Open a Session from configured preset defaults plus proto-JSON overrides.

        Retrying with the same Session ID and overrides answers the same Session.
        """
        opening = await self.open_session(name, session_id, overrides=overrides, setup_script=setup_script)
        return await self.wait_for_session_admission(name, session_id, opening=opening)

    @_traced
    async def open_session(
        self,
        name: str,
        session_id: UUID,
        *,
        overrides: dict[str, object] | None = None,
        setup_script: str | None = None,
    ) -> session_dispatcher_pb2.SessionOpening:
        """Submit an opening and return its first durable state without waiting for admission."""
        body = NewSession(session_id=session_id, spec=overrides or {}, setup_script=setup_script)
        answered = await self._json(
            "POST", f"/sandboxes/{name}/sessions", json=body.model_dump(mode="json", exclude_none=True)
        )
        opening = ParseDict(answered, session_dispatcher_pb2.SessionOpening())
        self.opened_sandbox_uids.add(UUID(opening.sandbox_uid))
        return opening

    async def session_opening(self, name: str, session_id: UUID) -> session_dispatcher_pb2.SessionOpening:
        """Read the durable opening state for a Sandbox Session."""
        answered = await self._json("GET", f"/sandboxes/{name}/sessions/{session_id}/opening")
        return ParseDict(answered, session_dispatcher_pb2.SessionOpening())

    @_traced
    async def wait_for_session_admission(
        self,
        name: str,
        session_id: UUID,
        *,
        opening: session_dispatcher_pb2.SessionOpening,
        timeout_s: float = REQUEST_SECONDS,
    ) -> None:
        """Wait for the runner admission behind an already accepted Session opening."""
        deadline = time.monotonic() + timeout_s
        while True:
            if opening.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED:
                return
            if opening.state in (
                session_dispatcher_pb2.DELIVERY_STATE_FAILED,
                session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
            ):
                raise RuntimeError(f"Session opening {opening.session_id} ended in {opening.state}: {opening.reason}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Session opening {opening.session_id} remained {opening.state}: {opening.reason}")
            await asyncio.sleep(0.2)
            opening = await self.session_opening(name, session_id)

    @_traced
    async def wait_for_thread_feed(self, thread_id: UUID, *, timeout_s: float) -> ThreadView:
        """Wait until History has folded the runner's first feed announcement into the Thread."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                thread = await self.thread(thread_id)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != HTTPStatus.NOT_FOUND:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Thread {thread_id} was not projected within {timeout_s:g}s") from error
            else:
                if thread.feed_status is not None:
                    return thread
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Thread {thread_id} received no runner feed within {timeout_s:g}s")
            await asyncio.sleep(0.2)

    @_traced
    async def wait_for_thread_projection(self, thread_id: UUID, *, timeout_s: float) -> ThreadView:
        """Wait for History's projection after runner admission; only a missing row is retried."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return await self.thread(thread_id)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != HTTPStatus.NOT_FOUND:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Thread {thread_id} was not projected within {timeout_s:g}s") from error
                await asyncio.sleep(0.2)

    @_traced_stream
    async def threads(self, *, sandbox_uid: UUID) -> AsyncGenerator[ThreadView]:
        """Every retained Thread of this Sandbox incarnation, including archived Threads."""
        params = {"sandbox_uid": str(sandbox_uid), "include_archived": "true"}
        while True:
            page = ThreadPage.model_validate(await self._json("GET", "/threads", params=params))
            for thread in page.threads:
                yield thread
            if page.next_page_token is None:
                return
            params["page_token"] = page.next_page_token

    async def thread(self, thread_id: UUID) -> ThreadView:
        """The persistent Thread of a created Session; its ID is the Session's public ID."""
        return ThreadView.model_validate(await self._json("GET", f"/threads/{thread_id}"))

    @_traced
    async def submit_input(
        self, thread_id: UUID, command_id: str, text: str
    ) -> session_dispatcher_pb2.Dispatch | event_log_pb2.EventEntry:
        """Submit an input by its stable ID and return durable or archived admission evidence."""
        answered = await self._json(
            "POST", f"/threads/{thread_id}/submit-input", json={"commandId": command_id, "text": text}
        )
        match answered["kind"]:
            case "dispatch":
                return ParseDict(answered["dispatch"], session_dispatcher_pb2.Dispatch())
            case "archived_admission":
                return ParseDict(answered["admission"], event_log_pb2.EventEntry())
            case kind:
                raise ValueError(f"unrecognized command submission kind: {kind!r}")

    @_traced_stream
    async def events(
        self, thread_id: UUID, *, after: int, read_seconds: float, awaiting_command_id: str | None = None
    ) -> AsyncGenerator[event_log_pb2.EventEntry]:
        """The Thread's events from `after`, ending when the app sends the `end` frame, and not before
        the admission of `awaiting_command_id` (a command submitted after `after`).

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
                params: dict[str, str | int] = {"after": cursor}
                if awaiting_command_id is not None:
                    params["awaiting_command_id"] = awaiting_command_id
                async with self._http.stream(
                    "GET", url, params=params, headers={"Accept": "text/event-stream"}, timeout=timeout
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
                                if (
                                    entry.event.HasField("command_admitted")
                                    and entry.event.command_admitted.command.command_id == awaiting_command_id
                                ):
                                    # A reopened stream starts past the admission, so it must not await it.
                                    awaiting_command_id = None
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
