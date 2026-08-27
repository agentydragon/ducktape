"""A connected client for Codex app-server's newline-delimited JSON-RPC-shaped protocol.

One app-server process owns one ephemeral thread for the lifetime of its sandbox.  The outer Haku
runner can reconnect a replacement Console to that same process, so ``connect`` first probes loaded
threads: a new process answers "Not initialized" and receives the handshake/thread start; an
adopted process returns the one thread the departed Console already opened.

Every native frame is durably recorded before routing.  Responses satisfy local requests,
server-initiated requests are refused, and notifications form the unbounded session stream consumed
by the generic turn loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from haku.console.x.codex_app_server import frames
from haku.console.x.codex_app_server.config import CodexApprovalPolicy, CodexReasoningEffort, CodexSandboxMode
from haku.console.x.codex_app_server.protocol import Notification, Request, RequestId, Response, parse_message
from haku.console.x.runtime import RuntimeClient
from haku.runtime.x.bridge.client import FrameSink, ReceivedFrame, SentPrompt
from haku.runtime.x.bridge.protocol import HarnessFrame, HarnessLaunch, TextWebSocket
from haku.runtime.x.bridge.transport import ProgressSink, WebSocketTransport

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 60.0


class FrameChannel(Protocol):
    async def connect(self) -> None: ...

    async def write(self, frame: HarnessFrame) -> None: ...

    def read_messages(self) -> AsyncIterator[HarnessFrame]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CodexThread:
    """Native app-server choices for the one thread behind a Haku session."""

    cwd: str
    model: str | None = None
    reasoning_effort: CodexReasoningEffort | None = None
    developer_instructions: str | None = None
    approval_policy: CodexApprovalPolicy = CodexApprovalPolicy.NEVER
    # The containment posture. Full access is deliberate for the runtime pod: the sandbox
    # boundary is the pod itself, not Codex's own jail.
    sandbox: CodexSandboxMode = CodexSandboxMode.DANGER_FULL_ACCESS
    ephemeral: bool = True

    def start_params(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
            "sandbox": self.sandbox,
            "ephemeral": self.ephemeral,
        }
        if self.model is not None:
            result["model"] = self.model
        if self.reasoning_effort is not None:
            # thread/start has no dedicated effort param at 0.144.1; its `config` map carries
            # per-thread overrides in config-file vocabulary, and `model_reasoning_effort` is the
            # exact key the app-server itself re-applies for a persisted thread's effort
            # (codex-rs/app-server/src/request_processors/thread_processor.rs). The thread/start
            # response echoes the effective value as `reasoningEffort`.
            result["config"] = {"model_reasoning_effort": self.reasoning_effort}
        if self.developer_instructions is not None:
            result["developerInstructions"] = self.developer_instructions
        return result


class CodexAppServerError(Exception):
    """A native request failed or was never answered."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


CodexClientFactory = Callable[
    [TextWebSocket, HarnessLaunch, ProgressSink | None, FrameSink, CodexThread], RuntimeClient
]


class CodexAppServer:
    """One app-server process addressed over an already-authenticated bridge transport."""

    def __init__(
        self,
        channel: FrameChannel,
        frames_to: FrameSink,
        thread: CodexThread,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    ):
        self._channel = channel
        self._frames_to = frames_to
        self._thread = thread
        self._request_timeout = request_timeout
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}
        self._notifications: asyncio.Queue[ReceivedFrame | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        # A replacement Console shares the same app-server stdio stream with its predecessor.
        # Per-connection integer ids could therefore collide with a late response to the departed
        # client. Codex accepts string ids, so namespace every connection's sequence independently.
        self._request_namespace = uuid4().hex
        self._next_request_id = 1
        self._thread_id: str | None = None
        self._active_turn_id: str | None = None

    async def connect(self) -> Mapping[str, Any]:
        """Connect to a new app-server, or adopt the one thread in an initialized process."""
        await self._channel.connect()
        self._reader = asyncio.create_task(self._read())
        try:
            loaded = await self._request(frames.THREAD_LOADED_LIST, {})
        except CodexAppServerError as error:
            if "not initialized" not in str(error).casefold():
                raise
            initialized = await self._request(
                frames.INITIALIZE,
                {
                    "clientInfo": {"name": "haku_console", "title": "Haku Console", "version": "0.1.0"},
                    "capabilities": None,
                },
            )
            await self._notify(frames.INITIALIZED)
            started = await self._request(frames.THREAD_START, self._thread.start_params())
            self._thread_id = frames.nested_string(_object(started, "thread/start result"), "thread", "id")
            return _object(initialized, "initialize result")

        loaded_object = _object(loaded, "thread/loaded/list result")
        thread_ids = loaded_object.get("data")
        if not isinstance(thread_ids, list) or any(not isinstance(value, str) for value in thread_ids):
            raise CodexAppServerError("thread/loaded/list returned malformed data")
        if len(thread_ids) != 1:
            raise CodexAppServerError(f"expected one loaded Codex thread, found {len(thread_ids)}")
        self._thread_id = thread_ids[0]
        read = _object(
            await self._request(frames.THREAD_READ, {"threadId": self._thread_id, "includeTurns": True}),
            "thread/read result",
        )
        thread = read.get("thread")
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
            raise CodexAppServerError("thread/read returned malformed turns")
        active_turn_ids: list[str] = []
        for turn in turns:
            if turn.get("status") != "inProgress":
                continue
            turn_id = turn.get("id")
            if not isinstance(turn_id, str):
                raise CodexAppServerError("thread/read returned a malformed active turn")
            active_turn_ids.append(turn_id)
        if len(active_turn_ids) > 1:
            raise CodexAppServerError("thread/read returned multiple active turns")
        self._active_turn_id = active_turn_ids[0] if active_turn_ids else None
        return {"threadId": self._thread_id, "activeTurnId": self._active_turn_id, "adopted": True}

    async def query(self, text: str) -> SentPrompt:
        if self._thread_id is None:
            raise CodexAppServerError("Codex app-server is not connected")
        if self._active_turn_id is not None:
            raise CodexAppServerError("Codex app-server already has an active turn")
        frame_seq, result = await self._request_with_frame_seq(
            frames.TURN_START,
            {"threadId": self._thread_id, "input": [{"type": "text", "text": text, "text_elements": []}]},
        )
        self._active_turn_id = frames.nested_string(_object(result, "turn/start result"), "turn", "id")
        return SentPrompt(frame_seq=frame_seq)

    async def interrupt(self) -> None:
        if self._thread_id is None or self._active_turn_id is None:
            return
        await self._request(frames.TURN_INTERRUPT, {"threadId": self._thread_id, "turnId": self._active_turn_id})

    async def frames(self) -> AsyncIterator[ReceivedFrame]:
        while (received := await self._notifications.get()) is not None:
            yield received

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def aclose(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        self._fail_pending()
        await self._channel.close()

    async def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        _, result = await self._request_with_frame_seq(method, params)
        return result

    async def _request_with_frame_seq(self, method: str, params: Mapping[str, Any]) -> tuple[int, Any]:
        request_id = f"haku-{self._request_namespace}-{self._next_request_id}"
        self._next_request_id += 1
        pending: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = pending
        try:
            frame_seq = await self._write({"method": method, "id": request_id, "params": dict(params)})
            return frame_seq, await asyncio.wait_for(pending, timeout=self._request_timeout)
        except TimeoutError as error:
            raise CodexAppServerError(f"request {method} was never answered") from error
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> int:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = dict(params)
        return await self._write(payload)

    async def _write(self, payload: dict[str, Any]) -> int:
        frame = HarnessFrame(frame=payload)
        frame_seq = await self._frames_to.sent(frame)
        await self._channel.write(frame)
        return frame_seq

    async def _read(self) -> None:
        skipped = 0
        try:
            async for envelope in self._channel.read_messages():
                recorded = await self._frames_to.received(envelope)
                if not recorded.fresh:
                    skipped += 1
                    continue
                if skipped:
                    logger.info("Skipped %d replayed Codex frame(s) already in the rollout", skipped)
                    skipped = 0
                message = parse_message(envelope.frame)
                if isinstance(message, Response):
                    self._resolve(message)
                elif isinstance(message, Request):
                    await self._refuse(message)
                else:
                    if isinstance(message, Notification) and message.method == frames.TURN_COMPLETED:
                        self._active_turn_id = None
                    # Unknown/future notifications belong in the native log and in fail-soft
                    # projection observability, so they travel with ordinary notifications.
                    self._notifications.put_nowait(ReceivedFrame(envelope=envelope, frame_seq=recorded.frame_seq))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex app-server stream failed")
        finally:
            if skipped:
                logger.info("Skipped %d replayed Codex frame(s) already in the rollout", skipped)
            self._fail_pending()
            self._notifications.put_nowait(None)
            self._closed.set()

    def _resolve(self, response: Response) -> None:
        pending = self._pending.get(response.request_id)
        if pending is None or pending.done():
            logger.debug("Ignoring a Codex response with no local waiter: %s", response.request_id)
            return
        if response.error is not None:
            code = response.error.get("code")
            message = response.error.get("message")
            pending.set_exception(
                CodexAppServerError(
                    str(message) if isinstance(message, str) else "app-server request failed",
                    code=code if isinstance(code, int) else None,
                )
            )
            return
        pending.set_result(response.result)

    def _fail_pending(self) -> None:
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(CodexAppServerError("the app-server connection closed"))
        self._pending.clear()

    async def _refuse(self, request: Request) -> None:
        logger.error("Codex app-server asked for %s, which this client does not serve", request.method)
        await self._write(
            {
                "id": request.request_id,
                "error": {"code": -32601, "message": f"{request.method} is not supported by this client"},
            }
        )


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodexAppServerError(f"{what} is not an object")
    return value


def app_server_over_websocket(
    websocket: TextWebSocket,
    launch: HarnessLaunch,
    on_progress: ProgressSink | None,
    frames_to: FrameSink,
    thread: CodexThread,
) -> CodexAppServer:
    """The Console composition: Codex app-server over the shared runner transport."""
    return CodexAppServer(WebSocketTransport(websocket, launch, on_progress), frames_to, thread)
