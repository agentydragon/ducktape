"""Codex app-server on the per-harness seam: its stateful handshake, turn loop, and projection.

`CodexHarness.run` owns everything native about a Codex session. It starts `codex app-server
--listen stdio://`, drives the stateful handshake (`initialize` → await response → `initialized` →
`thread/start` → capture the server-assigned `threadId`), and from there binds every turn to that
one thread. One thread per session: `thread/start` happens once, and each admitted prompt is a
`turn/start` on it.

Codex answers one turn at a time, so prompts are queued and started sequentially — a `turn/start`
only lands with no turn in flight, the next begins when `turn/completed` clears the active turn — and
the operator's interrupt is a `turn/interrupt` bound to the active `turnId`, sent out of band so it
need not wait behind a queued prompt. Notifications are projected to neutral operations by the
<codex_projection.py> `CodexProjector`; the neutral numbering, journal and admission fence are the
<session_api.py> `SessionApi`'s, not this module's.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

import anyio

from haku.runtime.x.bridge.backend import (
    ProcessLaunch,
    StdinWriter,
    child_environment,
    forward_stderr,
    read_json_frames,
    shutdown,
    start_process,
)
from haku.runtime.x.bridge.codex_options import (
    CODEX_DEVELOPER_INSTRUCTIONS_ENV,
    CODEX_MODEL_ENV,
    CODEX_REASONING_EFFORT_ENV,
    EXECUTABLE_VARIABLE,
)
from haku.runtime.x.bridge.codex_projection import CodexProjector
from haku.runtime.x.bridge.codex_protocol import (
    INITIALIZE,
    INITIALIZED,
    THREAD_START,
    TURN_COMPLETED,
    TURN_INTERRUPT,
    TURN_START,
    Notification,
    Request,
    RequestId,
    Response,
    nested_string,
    parse_message,
)
from haku.runtime.x.bridge.protocol import HarnessLaunch, Interrupt, PromptDispatch
from haku.runtime.x.bridge.session_api import SessionApi

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 60.0

# app-server thread/start posture. Full access is deliberate for the runtime pod: the sandbox
# boundary is the pod itself, not Codex's own jail. These occur once, so they stay string literals
# rather than a re-derived copy of Codex's pinned config enums.
_APPROVAL_POLICY = "never"
_SANDBOX = "danger-full-access"

_CLIENT_INFO = {"name": "haku_runner", "title": "Haku Runner", "version": "0.1.0"}


class CodexAppServerError(RuntimeError):
    """A native request failed or its connection closed before an answer."""


@dataclass(frozen=True, slots=True)
class CodexHarness:
    """Codex app-server, as the sandbox runner starts it, drives it, and reads it back."""

    name: ClassVar[str] = "codex-app-server"
    executable: Path

    def resolve(self, launch: HarnessLaunch) -> ProcessLaunch:
        return ProcessLaunch(
            executable=self.executable,
            arguments=launch.arguments,
            cwd=launch.cwd,
            environment=child_environment(launch),
        )

    async def run(self, launch: HarnessLaunch, session: SessionApi) -> None:
        process = await start_process(self.resolve(launch))
        stdout, stderr, raw_stdin = process.stdout, process.stderr, process.stdin
        assert stdout is not None
        assert stderr is not None
        assert raw_stdin is not None
        conversation = _CodexConversation(session, StdinWriter(raw_stdin), _thread_params(launch))
        stream_ended = anyio.Event()
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(forward_stderr, stderr, session)

                async def read_stream() -> None:
                    try:
                        async for payload in read_json_frames(stdout):
                            await conversation.on_frame(payload)
                    finally:
                        conversation.fail_pending()
                        stream_ended.set()

                # The reader runs before the handshake, so `initialize`/`thread/start` responses
                # resolve; once the thread exists, the command loop can turn prompts into turns.
                tasks.start_soon(read_stream)
                await conversation.handshake()
                tasks.start_soon(conversation.serve_commands)
                await stream_ended.wait()
                await session.flushed()
                tasks.cancel_scope.cancel()
        finally:
            # Shielded: the runner cancels this harness when the console gives up, and the process
            # still has to be reaped rather than leaked.
            with anyio.CancelScope(shield=True):
                exited_with = await shutdown(process)
        if exited_with not in (0, None):
            raise RuntimeError(f"{self.name} exited with status {exited_with}")


class _CodexConversation:
    """One Codex thread's live state: request correlation, the active turn, and the prompt queue."""

    def __init__(self, session: SessionApi, stdin: StdinWriter, thread_params: dict[str, Any]):
        self._session = session
        self._stdin = stdin
        self._thread_params = thread_params
        self._projector = CodexProjector()
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}
        self._queued_prompts: deque[PromptDispatch] = deque()
        self._thread_id: str | None = None
        # The app-server's id for the running turn, for `turn/interrupt`; distinct from the neutral
        # turn id the projector mints. None between turns.
        self._active_turn_id: str | None = None
        self._starting_turn = False
        self._next_request_id = 1

    async def handshake(self) -> None:
        """`initialize` → `initialized` → `thread/start`, capturing the one thread's id."""
        await self._request(INITIALIZE, {"clientInfo": _CLIENT_INFO, "capabilities": None})
        await self._notify(INITIALIZED)
        started = await self._request(THREAD_START, self._thread_params)
        self._thread_id = nested_string(_as_object(started, "thread/start result"), "thread", "id")

    async def serve_commands(self) -> None:
        """Turn the console's prompts and interrupts into native `turn/start`/`turn/interrupt`."""
        async for command in self._session.commands():
            match command:
                case PromptDispatch() as dispatch:
                    self._queued_prompts.append(dispatch)
                    await self._start_next_if_idle()
                case Interrupt():
                    await self._session.interrupt(self._compose_interrupt)

    async def on_frame(self, payload: dict[str, Any]) -> None:
        """Record one stdout frame, project its notification, and correlate a response or turn end."""
        message = parse_message(payload)
        reply = self._refuse(message) if isinstance(message, Request) else None
        await self._session.observe(payload, self._projector.observe, lambda _payload: reply)
        if reply is not None:
            await self._stdin.write_object(reply)
        if isinstance(message, Response):
            self._resolve(message)
        elif isinstance(message, Notification) and message.method == TURN_COMPLETED:
            self._active_turn_id = None
            await self._start_next_if_idle()

    async def _start_next_if_idle(self) -> None:
        """Start the next queued prompt if no turn is in flight; drains a duplicate without starting."""
        if self._starting_turn or self._active_turn_id is not None or not self._queued_prompts:
            return
        self._starting_turn = True
        try:
            await self._start_turn(self._queued_prompts.popleft())
        finally:
            self._starting_turn = False
        if self._active_turn_id is None:
            # The prompt was a duplicate the fence dropped; nothing is running, so try the next.
            await self._start_next_if_idle()

    async def _start_turn(self, dispatch: PromptDispatch) -> None:
        if self._thread_id is None:
            raise CodexAppServerError("a prompt was dispatched before the Codex thread started")
        request_id, future = self._register()
        input_item = {"type": "text", "text": dispatch.text, "text_elements": []}
        frame = {"method": TURN_START, "id": request_id, "params": {"threadId": self._thread_id, "input": [input_item]}}
        payload = await self._session.admit(
            dispatch.prompt_id, lambda: frame, partial(self._projector.admit, dispatch.prompt_id)
        )
        if payload is None:
            self._pending.pop(request_id, None)
            return
        await self._stdin.write_object(payload)
        try:
            result = await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_SECONDS)
        finally:
            self._pending.pop(request_id, None)
        self._active_turn_id = nested_string(_as_object(result, "turn/start result"), "turn", "id")

    def _compose_interrupt(self) -> dict[str, Any] | None:
        """The `turn/interrupt` for the running turn, or None when no turn is running.

        The response is not awaited: the abort is recorded by the fence's rewrite, and Codex reports
        it again as `turn/completed` with an `interrupted` status.
        """
        if self._thread_id is None or self._active_turn_id is None:
            return None
        return {
            "method": TURN_INTERRUPT,
            "id": self._mint_request_id(),
            "params": {"threadId": self._thread_id, "turnId": self._active_turn_id},
        }

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """One handshake request: inject it into the record, write it, await its response."""
        request_id, future = self._register()
        frame = {"method": method, "id": request_id, "params": params}
        await self._session.inject(frame)
        await self._stdin.write_object(frame)
        try:
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise CodexAppServerError(f"request {method} was never answered") from error
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str) -> None:
        frame = {"method": method}
        await self._session.inject(frame)
        await self._stdin.write_object(frame)

    def _register(self) -> tuple[str, asyncio.Future[Any]]:
        request_id = self._mint_request_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return request_id, future

    def _mint_request_id(self) -> str:
        request_id = f"haku-{self._next_request_id}"
        self._next_request_id += 1
        return request_id

    def _resolve(self, response: Response) -> None:
        future = self._pending.get(response.request_id)
        if future is None or future.done():
            logger.debug("Ignoring a Codex response with no local waiter: %s", response.request_id)
            return
        if response.error is not None:
            message = response.error.get("message")
            reason = message if isinstance(message, str) else "app-server request failed"
            future.set_exception(CodexAppServerError(reason))
        else:
            future.set_result(response.result)

    def _refuse(self, request: Request) -> dict[str, Any]:
        logger.error("Codex app-server asked for %s, which this runner does not serve", request.method)
        return {
            "id": request.request_id,
            "error": {"code": -32601, "message": f"{request.method} is not supported by this runner"},
        }

    def fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CodexAppServerError("the app-server connection closed"))
        self._pending.clear()


def _thread_params(launch: HarnessLaunch) -> dict[str, Any]:
    """The one thread's `thread/start` params, from the launch the console selected.

    Model, reasoning effort and developer instructions ride in the launch environment under
    `codex_options`' keys: the runner owns `thread/start` now, so what the console's `CodexThread`
    used to send it travels to the runner rather than staying console-side.
    """
    params: dict[str, Any] = {
        "cwd": launch.cwd,
        "approvalPolicy": _APPROVAL_POLICY,
        "sandbox": _SANDBOX,
        "ephemeral": True,
    }
    if model := launch.environment.get(CODEX_MODEL_ENV):
        params["model"] = model
    if effort := launch.environment.get(CODEX_REASONING_EFFORT_ENV):
        params["config"] = {"model_reasoning_effort": effort}
    if instructions := launch.environment.get(CODEX_DEVELOPER_INSTRUCTIONS_ENV):
        params["developerInstructions"] = instructions
    return params


def _as_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodexAppServerError(f"{what} is not an object")
    return value


def codex_harness(executable: Path | None = None) -> CodexHarness:
    """Codex at the image-selected path, or at *executable* for a test/local run."""
    return CodexHarness(
        executable=executable if executable is not None else Path(os.environ.get(EXECUTABLE_VARIABLE, "codex"))
    )
