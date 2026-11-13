"""HTTP client for the Go app and its generated EnvironmentService facade.

Session events arrive as shared `EventEntry` messages; consumers dispatch on
`entry.event.WhichOneof("observation")`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote
from uuid import UUID

import grpc
import httpx
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from tenacity import AsyncRetrying, retry_if_exception, stop_after_delay, wait_fixed

from agentplane.app import app_service_pb2
from agentplane.app.testing.models import ThreadMetadata, ThreadView, thread_view_from_proto_json
from agentplane.environments.v1 import environment_service_pb2
from agentplane.grpc.calls import ServiceError
from agentplane.proto import event_log_pb2, identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.

logger = logging.getLogger(__name__)

REQUEST_SECONDS = 30.0


# How long a Thread's stream may stay down, reconnecting, before the caller is told. Testing's app
# Deployment recreates its only replica, which took about three minutes to answer again (migrate
# initContainer, then startup) when its pod was deleted on 2026-10-13.
RECONNECT_SECONDS = 600.0
RECONNECT_PAUSE_SECONDS = 2.0
GRPC_UNAVAILABLE = 14
_ROLLING_STATUSES = frozenset({HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.GATEWAY_TIMEOUT})


def _no_app_answered(response: httpx.Response) -> bool:
    """What the public route answers while no app replica is ready: a 502/503/504, and on testing
    intermittently the proxy's own `403` with a plain-text "Access denied" body. The app's own
    refusals are JSON, so a JSON `403` is a verdict, not a gap."""
    return response.status_code in _ROLLING_STATUSES or (
        response.status_code == HTTPStatus.FORBIDDEN
        and response.headers.get("content-type", "").startswith("text/plain")
    )


def _thread_not_found(error: BaseException) -> bool:
    """History has not projected the Thread row yet."""
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == HTTPStatus.NOT_FOUND


class SessionStreamError(Exception):
    """The app ended a session's event stream with an error frame, or it stayed down too long."""


class _StreamDroppedError(Exception):
    """The stream's replica went away; the stream is to be reopened after its last entry."""


class Client:
    """Async context manager over one deployment of the app."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self.opened_sandbox_uids: set[str] = set()
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(REQUEST_SECONDS),
            follow_redirects=False,
            event_hooks={"request": [self._inject_trace_context]},
        )

    @staticmethod
    async def _inject_trace_context(request: httpx.Request) -> None:
        TraceContextTextMapPropagator().inject(request.headers)

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
        response = await self._json("GET", "/upstream/app-config/v1/models")
        return ModelCatalog.model_validate(
            {
                "models": [
                    {
                        "model": model["model"],
                        "display_name": model["displayName"],
                        "reasoning_efforts": model["reasoningEfforts"],
                    }
                    for model in response["models"]
                ],
                "harnesses": {offer["harness"]: offer["models"] for offer in response["harnesses"]},
            }
        )

    async def presets(self) -> app_service_pb2.ListSandboxPresetsResponse:
        response = await self._json("GET", "/upstream/app-config/v1/presets")
        return ParseDict(response, app_service_pb2.ListSandboxPresetsResponse())

    async def create_sandbox(
        self, spec: environment_service_pb2.CreateEnvironmentRequest
    ) -> identity_pb2.EnvironmentTarget:
        """Create once and return the exact incarnation acknowledged by the app."""
        target = ParseDict(
            await self._json("POST", "/upstream/environments/v1/environments", json=MessageToDict(spec)),
            identity_pb2.EnvironmentTarget(),
        )
        if not target.namespace or target.name != spec.name or not target.uid:
            raise RuntimeError("EnvironmentService returned an invalid created target")
        self.opened_sandbox_uids.add(target.uid)
        return target

    async def get_environment(
        self, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        """Read one authorized Sandbox target, fenced by its UID."""
        response = await self._json("GET", self._environment_path(target), params={"target.uid": target.uid})
        return ParseDict(response, environment_service_pb2.EnvironmentDescriptor())

    async def runner_status(self, name: str, uid: str) -> session_pb2.EnvironmentRuntimeStatus:
        response = await self._json("GET", f"/sandboxes/{name}/runner-status", params={"sandbox_uid": uid})
        return ParseDict(response, session_pb2.EnvironmentRuntimeStatus())

    async def sandbox_snapshots(self) -> AsyncGenerator[list[environment_service_pb2.EnvironmentDescriptor]]:
        """Yield complete inventories reconstructed from the EnvironmentService watch."""

        def key(environment: environment_service_pb2.EnvironmentDescriptor) -> tuple[str, str]:
            if (
                not environment.HasField("target")
                or not environment.target.namespace
                or not environment.target.name
                or not environment.target.uid
            ):
                raise RuntimeError("EnvironmentService returned a descriptor without its target")
            return environment.target.namespace, environment.target.name

        while True:
            pending: dict[tuple[str, str], environment_service_pb2.EnvironmentDescriptor] | None = {}
            current: dict[tuple[str, str], environment_service_pb2.EnvironmentDescriptor] = {}
            reconnect = False
            async with self._http.stream(
                "GET",
                "/upstream/environments/v1/environments:watch",
                headers={"Accept": "application/x-ndjson"},
                timeout=httpx.Timeout(REQUEST_SECONDS, read=None),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    envelope = json.loads(line)
                    if "error" in envelope:
                        if envelope["error"].get("code") == GRPC_UNAVAILABLE:
                            reconnect = True
                            break
                        raise RuntimeError(f"Environment watch failed: {envelope['error']}")
                    frame = ParseDict(envelope["result"], environment_service_pb2.WatchEnvironmentsResponse())
                    match frame.WhichOneof("frame"):
                        case "snapshot":
                            if pending is None:
                                raise RuntimeError("unexpected inventory snapshot chunk")
                            for environment in frame.snapshot.environments:
                                pending[key(environment)] = environment
                        case "snapshot_complete":
                            if pending is None:
                                raise RuntimeError("unexpected inventory snapshot completion")
                            current, pending = pending, None
                            yield [current[key] for key in sorted(current)]
                        case "upsert":
                            if pending is not None:
                                raise RuntimeError("inventory update before snapshot completion")
                            environment = frame.upsert
                            current[key(environment)] = environment
                            yield [current[key] for key in sorted(current)]
                        case "removed":
                            if pending is not None:
                                raise RuntimeError("inventory removal before snapshot completion")
                            removed = frame.removed
                            removed_key = (removed.namespace, removed.name)
                            previous = current.get(removed_key)
                            if previous is not None and previous.target.uid == removed.uid:
                                del current[removed_key]
                                yield [current[key] for key in sorted(current)]
                        case _:
                            raise RuntimeError("Environment watch returned an empty frame")
            if not reconnect:
                raise RuntimeError("the Sandbox inventory stream ended")
            await asyncio.sleep(RECONNECT_PAUSE_SECONDS)

    async def suspend_sandbox(self, target: identity_pb2.EnvironmentTarget) -> None:
        request = environment_service_pb2.EnvironmentMutationRequest(target=target)
        await self._json("POST", f"{self._environment_path(target)}:suspend", json=MessageToDict(request))

    async def resume_sandbox(self, target: identity_pb2.EnvironmentTarget) -> None:
        request = environment_service_pb2.ResumeEnvironmentRequest(target=target)
        data = await self._json("POST", f"{self._environment_path(target)}:resume", json=MessageToDict(request))
        response = ParseDict(data, environment_service_pb2.ResumeEnvironmentResponse())
        if response.target != target or response.outcome not in (
            environment_service_pb2.RESUME_OUTCOME_REQUESTED,
            environment_service_pb2.RESUME_OUTCOME_ALREADY_RUNNING,
        ):
            raise RuntimeError(f"Environment resume was not accepted: {response}")

    async def delete_sandbox(self, target: identity_pb2.EnvironmentTarget) -> None:
        await self._json("DELETE", self._environment_path(target), params={"target.uid": target.uid})

    @staticmethod
    def _environment_path(target: identity_pb2.EnvironmentTarget) -> str:
        if not target.namespace or not target.name or not target.uid:
            raise ValueError("complete EnvironmentService target handle is required")
        return (
            f"/upstream/environments/v1/environments/{quote(target.namespace, safe='')}/{quote(target.name, safe='')}"
        )

    async def create_session_and_wait(
        self,
        name: str,
        session_id: UUID,
        *,
        dispatcher: SessionDispatcherClient,
        sandbox_uid: str,
        overrides: dict[str, object] | None = None,
        setup_script: str | None = None,
    ) -> None:
        """Create a Session, then wait for the Dispatcher admission by its global ID.

        Retrying with the same Session ID and overrides answers the same Session.
        """
        session = await self.create_session(
            name, session_id, sandbox_uid=sandbox_uid, overrides=overrides, setup_script=setup_script
        )
        await self.wait_for_session_acceptance(dispatcher, session_id, session=session)

    async def create_session(
        self,
        name: str,
        session_id: UUID,
        *,
        sandbox_uid: str,
        overrides: dict[str, object] | None = None,
        setup_script: str | None = None,
    ) -> session_dispatcher_pb2.Session:
        """Create once and return its first durable materialization state."""
        body = app_service_pb2.CreateSessionRequest(
            session_id=str(session_id), sandbox_uid=sandbox_uid, spec=overrides or {}
        )
        if setup_script is not None:
            body.setup_script.string_value = setup_script
        answered = await self._json(
            "POST", f"/sandboxes/{name}/sessions", json=MessageToDict(body, preserving_proto_field_name=True)
        )
        session = ParseDict(answered, session_dispatcher_pb2.Session())
        match session.placement.WhichOneof("kind"):
            case "environment_hosted":
                target = session.placement.environment_hosted.environment
            case "central_sampler_v1":
                target = session.placement.central_sampler_v1.executor_environment
            case _:
                raise RuntimeError("Session omitted its exact Environment placement")
        if target.name != name or not sandbox_uid or target.uid != sandbox_uid:
            raise RuntimeError("Session creation acknowledged a different Environment identity")
        self.opened_sandbox_uids.add(target.uid)
        return session

    async def wait_for_session_acceptance(
        self,
        dispatcher: SessionDispatcherClient,
        session_id: UUID,
        *,
        session: session_dispatcher_pb2.Session,
        timeout_s: float = REQUEST_SECONDS,
    ) -> None:
        """Wait until the Dispatcher observes acceptance by the placement owner."""
        deadline = time.monotonic() + timeout_s
        while True:
            if session.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED:
                return
            if session.status.materialization in (
                session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_FAILED,
                session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_UNKNOWN,
            ):
                raise RuntimeError(
                    f"Session {session.session_id} ended in {session.status.materialization}: {session.status.reason}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Session {session.session_id} remained in {session.status.materialization}: {session.status.reason}"
                )
            await asyncio.sleep(0.2)
            try:
                session = await dispatcher.get_session(str(session_id))
                if session.session_id != str(session_id):
                    raise RuntimeError(
                        f"Dispatcher returned Session {session.session_id} while waiting for Session {session_id}"
                    )
            except ServiceError as error:
                if error.code != grpc.StatusCode.UNAVAILABLE:
                    raise

    async def wait_for_thread_feed(self, thread_id: UUID, *, timeout_s: float) -> ThreadView:
        """Wait until History has folded the runner's first feed announcement into the Thread."""
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(timeout_s),
            wait=wait_fixed(0.2),
            retry=retry_if_exception(lambda error: isinstance(error, AssertionError) or _thread_not_found(error)),
            reraise=True,
        ):
            with attempt:
                thread = await self.thread(thread_id)
                assert thread.feed_status is not None, f"Thread {thread_id} has no runner feed yet"
        return thread

    async def wait_for_thread_projection(self, thread_id: UUID, *, timeout_s: float) -> ThreadView:
        """Wait for History's projection after runner admission; only a missing row is retried."""
        return await AsyncRetrying(
            stop=stop_after_delay(timeout_s),
            wait=wait_fixed(0.2),
            retry=retry_if_exception(_thread_not_found),
            reraise=True,
        )(self.thread, thread_id)

    async def threads(self, *, sandbox_uid: str) -> AsyncGenerator[ThreadView]:
        """Every retained Thread of this Sandbox incarnation, including archived Threads."""
        params = {"include_archived": "true", "limit": "200"}
        while True:
            page = await self._json("GET", "/upstream/app/v1/threads", params=params)
            for row in page.get("threads", []):
                metadata = ThreadMetadata.model_validate(row)
                try:
                    summary = await self._json("GET", f"/upstream/history/v1/sessions/{metadata.thread_id}/summary")
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == HTTPStatus.NOT_FOUND:
                        continue
                    raise
                history_sandbox = summary.get("sandbox") or {}
                if history_sandbox.get("uid") == sandbox_uid:
                    yield thread_view_from_proto_json(row, summary)
            page_token = page.get("nextPageToken")
            if not page_token:
                return
            params["page_token"] = page_token

    async def thread(self, thread_id: UUID) -> ThreadView:
        """The persistent Thread of a created Session; its ID is the Session's public ID."""
        metadata = await self._json("GET", f"/upstream/app/v1/threads/{thread_id}")
        summary = await self._json("GET", f"/upstream/history/v1/sessions/{thread_id}/summary")
        return thread_view_from_proto_json(metadata, summary)

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
