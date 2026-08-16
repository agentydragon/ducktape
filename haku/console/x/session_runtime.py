"""Operator chat sessions backed by Claude Code in Agent Sandbox pods.

The service half: the turn loop, the runner's websocket bridge, the sandbox lifecycle and the SPA
chat surface's own routes. The rows underneath it, and every transaction that moves them, are
`session_store.py`.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr

from haku.console.chat_models import (
    ENDED_SESSION_STATUSES,
    FrameDirection,
    RecordedToolCall,
    SessionStatus,
    TurnOutcome,
)
from haku.console.config import ClaudeRuntimeConfig
from haku.console.operator_auth import OperatorActorDep

# As a module: its `RecordedFrame` is a row of the frame log, and `cli_client`'s is where a sink
# put one frame. Two different things with one name, so neither gets to drop its surname here.
from haku.console.x import claude_projection
from haku.console.x.conversation_events import (
    ActivityCompleted,
    ActivityStarted,
    ConversationEvent,
    MessageCompleted,
    Reasoning,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from haku.console.x.room_status import TurnStatus, ignore_clear, ignore_status
from haku.console.x.sandbox_claims import ProvisioningStep, SandboxClaims, provisioning_view
from haku.console.x.session_frames import SETUP_OUTPUT_KIND, frame_kind, setup_output_frame
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_store import (
    LEASE_RENEW_INTERVAL,
    BridgeAuthentication,
    ResumedTurn,
    SessionStore,
    SessionSurface,
    SpaSession,
    TurnStart,
)
from haku.console.x.session_views import (
    DEFAULT_FRAME_PAGE,
    MAX_FRAME_PAGE,
    ConversationSessionSummary,
    ConversationSessionView,
    SessionFramePage,
    SessionMessageView,
    SessionView,
)
from haku.runtime.x.claude_bridge.cli_client import (
    ClaudeCli,
    ReceivedFrame,
    RecordedFrame,
    SentPrompt,
    cli_over_websocket,
)
from haku.runtime.x.claude_bridge.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runtime.x.claude_bridge.protocol import GOING_AWAY_CODE, NOT_ADMITTED_CODE, TextWebSocket

router = APIRouter(tags=["sessions"])
internal_router = APIRouter(tags=["claude-chat-internal"])
logger = logging.getLogger(__name__)

# Appended to a turn's stored answer when the operator stopped it, and sent on its own when the
# room has already heard the turn's prose — so an abort is visible either way.
ABORTED_NOTICE = "[aborted by operator]"

# Stands in for the rollout sequence of a frame that has none, so it can be projected at all — see
# `_projected`. It never reaches a row: what the turn loop writes is the sequence it was handed.
_UNNUMBERED_FRAME = -1


def _first_message(errors: BaseExceptionGroup[Exception]) -> str:
    """The message of the first leaf in *errors*, for the operator-facing `error` column.

    `except*` hands back a group even when one thing failed, and a group's own `str` is a
    count ("1 sub-exception"), which says nothing about what broke.
    """
    leaves = errors.exceptions
    while leaves and isinstance(leaves[0], BaseExceptionGroup):
        leaves = leaves[0].exceptions
    return str(leaves[0]) if leaves else str(errors)


class SessionPromptRequest(BaseModel):
    """What the SPA posts to send a prompt. Named for the request, since the prompt itself is now
    a row (`database_schema.SessionPrompt`) rather than a field on the way in."""

    text: str = Field(min_length=1, max_length=100_000)


class TurnClient(Protocol):
    """The part of the CLI client the turn loop needs after frames are handed to it."""

    async def query(self, text: str) -> SentPrompt: ...

    async def interrupt(self) -> None: ...


class StarletteTextWebSocket(TextWebSocket):
    def __init__(self, websocket: WebSocket):
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        await self._websocket.send_text(data)

    async def receive_text(self) -> str:
        return await self._websocket.receive_text()

    async def close(self) -> None:
        await self._websocket.close()


class RolloutRecorder:
    """One session's `FrameSink`: every protocol frame either way, into `session_frames`.

    **No exclusions.** Control frames are kept because an interrupt that did not take is only
    diagnosable from them, and deltas because a log with a hole in it cannot be folded over
    (<../../plans/chat_runtime_projection.md>). `read_frames` is where "do not bury the reader"
    is answered, by leaving deltas out of its default view.
    """

    def __init__(self, store: SessionStore, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, payload: dict[str, Any]) -> int:
        return (await self._record(FrameDirection.TO_AGENT, payload)).frame_seq

    async def received(self, payload: dict[str, Any]) -> RecordedFrame:
        """Record the frame, answering whether the caller should act on it and where it landed.

        A delta has no agent-assigned identity, so it is always recorded and always fresh — safe
        because the runner never replays one (`runner.DELTA_TYPE`).
        """
        return await self._record(FrameDirection.FROM_AGENT, payload)

    async def _record(self, direction: FrameDirection, payload: dict[str, Any]) -> RecordedFrame:
        return await self._store.record_frame(self._session_id, direction, frame_kind(payload), payload)


class RoomSurface(Protocol):
    """The front end for sessions that serve a room, for the parts a turn cannot do itself.

    The SPA needs none of this — its client reads the message rows over SSE, so a finished turn is
    delivered by being written down. A room has to be spoken to.

    **The service picks this by reading the session's `surface`**, rather than offering every
    session to every listener and letting each one re-derive whether it is its own.

    **Replies are not here any more.** They are rows in `session_outbox`, written where they are
    produced and drained into the room by whoever holds the outbox lock, which is what a surface
    reporting success at enqueue could never be (<../debug/message_drops.md>). What is left is
    what genuinely describes a moment and is worthless afterwards.
    """

    async def system_prompt(self, session_id: UUID, room_id: str) -> str: ...

    async def report_silent_turn(self, room_id: str) -> None: ...

    async def report(self, room_id: str, detail: str) -> None: ...

    async def show_status(self, room_id: str, text: str) -> None: ...

    async def clear_status(self, room_id: str) -> None: ...

    async def set_typing(self, room_id: str, active: bool) -> None: ...


class SessionService:
    def __init__(
        self,
        config: ClaudeRuntimeConfig,
        store: SessionStore,
        claims: SandboxClaims,
        notifications: SessionNotifications,
        *,
        mcp_token: SecretStr,
        room_surface: RoomSurface | None = None,
    ):
        self._config = config
        self._store = store
        self._claims = claims
        self._notifications = notifications
        self._mcp_token = mcp_token
        self._room_surface = room_surface

    async def request_abort(self, operator_id: UUID, session_id: UUID) -> bool:
        """Interrupt this session's turn, or answer False when it has none.

        Raises `KeyError` for a session this Operator does not own, so the route asks one
        question instead of reaching through `service._store` for an ownership check and then
        asking the service for the abort.
        """
        if not await self._store.session_exists(operator_id, session_id):
            raise KeyError(session_id)
        return await self._store.request_abort(session_id)

    async def create(self, operator_id: UUID, surface: SessionSurface) -> SessionView:
        view, token = await self._store.create(operator_id, surface)
        try:
            await self._claims.create(
                session_id=view.session_id,
                bridge_token=token,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._config.session_ttl_seconds),
            )
        except Exception as error:
            await self._store.fail(view.session_id, f"sandbox provisioning failed: {error}")
            # If claim creation reached Kubernetes before its response failed, remove the partial
            # resource now. A failed delete leaves the rendezvous hash as a durable retry marker.
            await self._cleanup_terminal_claim(view.session_id)
            raise
        return await self._with_provisioning(view)

    async def get(self, operator_id: UUID, session_id: UUID) -> SessionView:
        view = await self._store.get(operator_id, session_id)
        return await self._with_provisioning(view)

    async def _with_provisioning(self, view: SessionView) -> SessionView:
        if view.status != SessionStatus.PROVISIONING:
            return view
        try:
            provisioning = await self._claims.inspect(session_id=view.session_id)
        except Exception as error:
            provisioning = provisioning_view(
                f"claude-{view.session_id.hex}", step=ProvisioningStep.CLAIM_CREATED, observation_error=str(error)
            )
        return view.model_copy(update={"provisioning": provisioning})

    async def dispose(self, operator_id: UUID, session_id: UUID) -> None:
        await self._store.request_close(operator_id, session_id)
        await self._claims.delete(session_id=session_id)
        await self._store.complete_claim_cleanup(session_id)

    async def reconcile_terminal_claims(self) -> None:
        """Finish idempotent claim cleanup left behind by an interrupted Console process."""

        session_ids = await self._store.claim_cleanup_candidates()
        for session_id in session_ids:
            await self._cleanup_terminal_claim(session_id)

    async def _cleanup_terminal_claim(self, session_id: UUID) -> bool:
        try:
            await self._claims.delete(session_id=session_id)
        except Exception as error:
            # Keep the credential fingerprint as a durable cleanup-pending marker so another
            # replica or a later restart retries. Kubernetes deletion is idempotent.
            logger.warning("Claude claim cleanup failed for session %s: %s", session_id, error)
            return False
        await self._store.complete_claim_cleanup(session_id)
        return True

    async def _room_of(self, session_id: UUID) -> str | None:
        """The room this session serves, or None for one that serves no room.

        Read once per runner connection and carried for the session's life: it is immutable on
        the row, so re-reading it would only add round trips.
        """
        return None if self._room_surface is None else await self._store.room_of(session_id)

    async def _appended_prompt(self, session_id: UUID, room_id: str | None) -> str | None:
        """Who this session is, appended to Claude Code's own system prompt.

        Appended rather than replacing it: the built-ins (Read, Bash, Edit) are live in the
        sandbox and the preset is what tells the model how to drive them. Haku's identity is an
        addition to that, not a substitute for it — which is why the launch sends
        `--append-system-prompt` and never `--system-prompt`.
        """
        if self._room_surface is None or room_id is None:
            return None
        return await self._room_surface.system_prompt(session_id, room_id)

    def _turn_status(self, room_id: str | None) -> TurnStatus:
        """A status driver for one turn, wired to the room if this session serves one.

        A session with no room still gets a driver rather than a `None` to branch on: the SPA
        reads the message rows, so there is simply nothing for its status to do, and the turn
        loop should not have to know which surface it is on.
        """
        surface, room = self._room_surface, room_id
        if surface is None or room is None:
            return TurnStatus(ignore_status, ignore_clear)
        return TurnStatus(
            lambda text: surface.show_status(room, text),
            lambda: surface.clear_status(room),
            lambda active: surface.set_typing(room, active),
        )

    def _progress_reporter(self, session_id: UUID, room_id: str | None) -> Callable[[str], Awaitable[None]]:
        """Record every sandbox progress report, log it, and show it to the room if there is one.

        Recorded first because the rollout is the only durable copy: the pod's log is reaped with
        the sandbox, and a session that died before its first CLI frame has its whole account here.
        """

        async def report(detail: str) -> None:
            logger.info("Claude sandbox %s: %s", session_id, detail)
            await self._store.record_frame(
                session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame(detail)
            )
            if self._room_surface is not None and room_id is not None:
                await self._room_surface.report(room_id, detail)

        return report

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == BridgeAuthentication.HELD:
            # **A denial response, not a close.** A websocket closed before `accept()` reaches the
            # client as HTTP 403 whatever code is passed to it (uvicorn renders every one that
            # way), so a refusal and a "not yet" were indistinguishable — and the runner gives up
            # on a 4xx, correctly, because a bad credential is not worth redialling. The ASGI
            # `websocket.http.response` extension is what lets this answer 503 instead, which the
            # runner already waits out: `_worth_redialling` retries anything 5xx, since that is
            # also what the Gateway says mid-roll.
            logger.info("session %s is held by another replica; telling the runner to retry", session_id)
            await websocket.send_denial_response(
                Response(status_code=503, content=b"session is held by another replica")
            )
            return
        if authentication == BridgeAuthentication.TERMINAL:
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=NOT_ADMITTED_CODE, reason="runner session is already terminal")
            return
        if authentication == BridgeAuthentication.REJECTED:
            await websocket.close(code=NOT_ADMITTED_CODE, reason="invalid or consumed runner credential")
            return
        # Whatever the previous holder was in the middle of *is* ours to finish: the sandbox
        # outlived it, so the rest of that exchange is about to arrive on this socket. What is
        # left to decide is which of three things its open turn was — `adopt_open_turn`.
        resumed = await self._store.adopt_open_turn(session_id)
        if resumed is not None:
            logger.warning("session %s adopted with turn %s still running", session_id, resumed.turn_id)
        # Rendered before the socket is accepted, alongside the other admission failures, so a
        # broken prompt ends the session where the supervisor can see it (and say so in the
        # room) instead of raising past the cleanup below and leaving the claim stranded.
        # Failing is deliberate: a session that silently started without its identity is the
        # generic-assistant bug this prompt exists to fix, and it would be invisible.
        try:
            room_id = await self._room_of(session_id)
            appended = await self._appended_prompt(session_id, room_id)
        except Exception as error:
            logger.exception("Claude system prompt failed to render for session %s", session_id)
            await self._store.fail(session_id, f"system prompt failed to render: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="system prompt failed to render")
            return
        await websocket.accept()
        session = ClaudeSession(
            append_system_prompt=appended,
            cwd=Path(self._config.cwd),
            environment=self._config.claude_environment(),
            mcp_servers={
                "haku-console": HttpMcpServer(
                    url=self._config.mcp_url, headers={"Authorization": f"Bearer {self._mcp_token.get_secret_value()}"}
                )
            },
        )
        client = cli_over_websocket(
            StarletteTextWebSocket(websocket),
            build_claude_launch(session),
            self._progress_reporter(session_id, room_id),
            RolloutRecorder(self._store, session_id),
        )
        abort_event = asyncio.Event()
        # Whether the sandbox should outlive this connection. False for an ending session — one
        # closed, or failed in a way the CLI cannot be asked to continue past — and true when it
        # is only this replica that is going away.
        keep_sandbox = False
        # Two nested handlers because Python forbids `except` and `except*` on one `try`, and
        # the two are about different things: the inner one unwraps whatever the task group
        # failed with, the outer one is this whole activity being cancelled.
        try:
            try:
                # `TaskGroup` rather than bare `create_task`: both helpers run for exactly this
                # block's lifetime, and it owns awaiting and cancelling them.
                async with asyncio.TaskGroup() as helpers:
                    # The operator's abort lands on whichever replica the Service picks, which
                    # is rarely the one holding this websocket — so the event is driven by
                    # NOTIFY, not by a caller reaching into this process.
                    abort_watch = helpers.create_task(self._watch_aborts(session_id, abort_event))
                    # Says "this replica is still here" for as long as it is. Its absence is
                    # what another replica reclaims the session by; see `expire_stale_leases`.
                    renewal = helpers.create_task(self._renew_lease(session_id))
                    # Turns the runner's socket dropping into a `WebSocketDisconnect` here, even
                    # when this handler is parked in the idle prompt-wait with nothing reading the
                    # socket. Without it a roll leaves an idle session waiting out the whole
                    # graceful-shutdown timeout before it hands back.
                    connection = helpers.create_task(self._watch_connection(client))
                    try:
                        await client.connect()
                        # One stream for the session, not one per turn: a folded prompt is
                        # answered with no second `result`, and an adopted turn was issued by a
                        # process that is gone. So a turn is a bracket over this stream rather
                        # than a request/response pair
                        # (<../../plans/cli_protocol_ownership.md>).
                        frames = client.frames().__aiter__()
                        while True:
                            status = await self._store.status(session_id)
                            if status is None or status in ENDED_SESSION_STATUSES:
                                break
                            # The inherited turn before any new prompt, and once: its remaining
                            # frames are already on their way, so opening a second turn to take
                            # them would deliver one exchange's answer into another's bracket —
                            # which is what routing by session rather than by turn meant.
                            turn: TurnStart | ResumedTurn | None = resumed
                            resumed = None
                            if turn is None:
                                turn = await self._store.next_prompt(session_id)
                            if turn is None:
                                # Wait for a LISTEN/NOTIFY instead of polling.
                                await self._notifications.wait(
                                    SessionEventKind.PROMPT, session_id, timeout_seconds=30.0
                                )
                                continue
                            # Cleared before the turn, not after: an abort notified just as the
                            # previous one ended would otherwise sit set through the idle wait and
                            # kill this turn on arrival.
                            abort_event.clear()
                            try:
                                await self._run_turn(
                                    client, frames, session_id, turn, room_id=room_id, abort_event=abort_event
                                )
                            except Exception as error:
                                logger.exception("turn failed for session %s", session_id)
                                await self._store.fail(session_id, str(error))
                                break
                    finally:
                        # The helpers outlive the loop by construction, so ending it is what
                        # ends them; the group then awaits them before leaving this block.
                        abort_watch.cancel()
                        renewal.cancel()
                        connection.cancel()
            except* WebSocketDisconnect:
                # The runner went away, which is not the session being over: it keeps the CLI
                # alive across a lost socket and redials. Hand the session back and let the lease
                # decide — a runner that never returns leaves the row to the sweep.
                logger.info("session %s lost its runner; leaving it for adoption", session_id)
                keep_sandbox = True
                await self._store.release_lease(session_id)
            except* Exception as errors:
                # `fail` records the message; the traceback is what says which call produced
                # it, and the listener mismatch was three frames below anything it named.
                logger.exception("Claude runtime failed for session %s", session_id)
                await self._store.fail(session_id, f"Claude runtime failed: {_first_message(errors)}")
        except asyncio.CancelledError:
            # `CancelledError` is a `BaseException`, so neither clause above sees it. This is
            # this replica going away — a rolling update, an evicted pod — which says nothing
            # about the session, so it must not be recorded as a failure: a terminal row refuses
            # the runner's reconnect and the supervisor builds a replacement.
            #
            # Hand it back instead. The sandbox outlives this process, the runner redials, and
            # whichever replica answers adopts it. Nothing is swallowed: the sweep fails the
            # session once its adoption window passes with no runner back.
            keep_sandbox = True
            await self._store.release_lease(session_id)
            raise
        finally:
            # Shielded because everything here is an `await` and this task may already be
            # cancelled, in which case the first one would re-raise and the rest would silently
            # not happen — which is how `closed()` came to be skipped. Best effort even so: a
            # SIGKILL runs no finalizer at all, which is why the lease, not this block, is what
            # actually guarantees the session stops looking alive.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.shield(
                    asyncio.wait_for(self._finalize(session_id, websocket, client, keep_sandbox), timeout=10)
                )

    async def _finalize(self, session_id: UUID, websocket: WebSocket, client: ClaudeCli, keep_sandbox: bool) -> None:
        """Let go of one runner connection, and of the session itself unless it outlives us.

        `keep_sandbox` is the difference between "this conversation is over" and "this replica
        is". Deleting the claim on the second is what made a roll destroy the sandbox it was
        supposed to leave running.
        """
        if keep_sandbox:
            # Said with a code rather than by dropping the socket, so the runner reconnects
            # because it was told to rather than because it guessed.
            with contextlib.suppress(Exception):
                await websocket.close(code=GOING_AWAY_CODE, reason="console replica going away")
            await client.aclose()
            return
        await client.aclose()
        await self._cleanup_terminal_claim(session_id)
        await self._store.closed(session_id)

    async def _renew_lease(self, session_id: UUID) -> None:
        """Hold *session_id*'s lease for as long as this replica runs it, and keep its sandbox with it.

        The same heartbeat slides the SandboxClaim's `shutdownTime` forward, so the sandbox is a
        renewed lease rather than a `session_ttl_seconds` hard timer that killed a conversation in
        full flow (`sandbox_claims.renew`). Console lease and sandbox deadline lapse together the
        moment a replica stops tending the session.
        """
        while True:
            await self._store.renew_lease(session_id)
            await self._claims.renew(
                session_id=session_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._config.session_ttl_seconds),
            )
            await asyncio.sleep(LEASE_RENEW_INTERVAL.total_seconds())

    async def _watch_connection(self, client: ClaudeCli) -> None:
        """Raise `WebSocketDisconnect` the moment the runner's stream ends.

        The reader is a detached task, so a dropped socket cannot propagate into the task group on
        its own — it becomes a `None` sentinel that only a *turn* consumer sees. An idle session is
        not consuming, so without this it sits in the prompt-wait until the graceful-shutdown timer
        cancels it. Waking here routes the drop to the `except* WebSocketDisconnect` clause that
        hands the session back, so a roll is handed back at once rather than after that timeout.
        """
        await client.wait_closed()
        raise WebSocketDisconnect(code=GOING_AWAY_CODE)

    async def _watch_aborts(self, session_id: UUID, abort_event: asyncio.Event) -> None:
        """Set *abort_event* every time this session is told to abort, until cancelled.

        The operator's abort lands on whichever replica the Service picks, which is rarely
        the one holding this session's websocket, so it arrives over NOTIFY rather than by a
        caller reaching into this process.
        """
        async with self._notifications.subscribe(SessionEventKind.ABORT, session_id) as notified:
            while True:
                await notified.wait()
                notified.clear()
                abort_event.set()

    async def _run_turn(
        self,
        client: TurnClient,
        frames: AsyncIterator[ReceivedFrame],
        session_id: UUID,
        turn: TurnStart | ResumedTurn,
        *,
        room_id: str | None,
        abort_event: asyncio.Event,
    ) -> None:
        """Ask *turn*'s question if it has not been asked, then consume the stream until the turn
        completes.

        **Project, then act.** Every frame goes through `claude_projection` and this loop acts on
        the neutral events that come back, so what it knows about is prose, messages, tool calls
        and a completed turn — not `assistant`, `stream_event` and `result`. That is the seam a
        second backend arrives through: another adapter into the same vocabulary, rather than a
        second reading of what a message is (<../../plans/chat_runtime_projection.md> § stage 4).

        *frames* belongs to the session, not to this call — see `handle_runner`. This call is the
        turn's span, so it closes it on every exit and is the only thing that does. A turn left
        open is therefore not a bookkeeping leak — it means no code got to close it, which is
        what a replica losing its pod mid-exchange looks like from outside, and what the
        `ResumedTurn` variant exists to pick back up.

        **The state below is the row's, held here only between two writes of it.** Every branch
        that changes one of these writes it back before the next frame is taken, so a process
        dying anywhere in this loop leaves `session_turns` saying what had happened — which is
        what makes adoption a read (<../../plans/chat_runtime_projection.md> § stage 3).
        """
        turn_id = turn.turn_id
        if isinstance(turn, TurnStart):
            # A resumed turn's question was asked by a process that is gone; only its answer is
            # still coming.
            prompt = await client.query(turn.prompt)
            # The prompt's row was written when the operator typed it, before any frame existed to
            # point at; this is where the question acquires the frame it went out as.
            await self._store.set_message_source_frames(session_id, turn.message_id, prompt.frame_seq)
        state = await self._store.turn_state(turn_id)
        assistant_id = state.assistant_message_id
        streamed = state.streamed
        # Whether this turn has already queued the room a reply, so the turn's final text is not
        # posted a second time: `result.result` normally repeats the last assistant message. It is
        # the outbox row's existence — recorded on the turn by the transaction that inserts the
        # row — and neither a report from the delivery layer, which used to be a statement about a
        # `deque.append`, nor `sent_at`, which is the drain's business and comes later.
        spoke = state.queued_reply
        # Its own fact rather than `spoke` again: a session with no room queues nothing, so a turn
        # that has completed a message there has `said_anything` and no `queued_reply` — and
        # minting a second message for `result.result` is exactly what conflating them did.
        saw_assistant_message = state.said_anything
        # The calls of the message being assembled, from the events that start them to the one
        # that completes it. Not turn state: they are written with their message, and a message
        # never spans two frames here (see `_projected`).
        tool_calls: list[RecordedToolCall] = []
        completed: TurnCompleted | None = None
        # The frame `completed` was projected from, kept because three things below still read
        # Claude's own payload out of it — and each is a piece of stage 4 this change leaves for
        # its successors: the failure's reason (a neutral outcome carries no message), the prose
        # of a turn that said nothing anywhere else, and the cost, usage and duration `end_turn`
        # stores verbatim. Appealing an event to the frame behind it is the design's own escape
        # hatch, so this is the seam working rather than leaking.
        result: dict[str, Any] | None = None
        result_frame_seq: int | None = None
        status = self._turn_status(room_id)
        status.start()
        aborted = asyncio.ensure_future(abort_event.wait())
        # Set once the abort has been seen and the CLI interrupted, from which point this loop
        # stops racing the abort event and drains what is left of the turn to its `result`.
        interrupted = False
        try:
            while completed is None:
                # Exactly one `anext` in flight, and the drain consumes the one it finds rather
                # than starting another: an async generator refuses to be advanced twice at once,
                # and an abort always arrives while this call is parked here.
                next_frame = asyncio.ensure_future(anext(frames))
                if not interrupted:
                    await asyncio.wait([next_frame, aborted], return_when=asyncio.FIRST_COMPLETED)
                    if interrupted := abort_event.is_set():
                        with contextlib.suppress(Exception):
                            await client.interrupt()
                # **The drain is this loop, not a second one beside it.** The CLI finishes the
                # message it is mid-way through, so an `assistant` frame between the interrupt and
                # the `result` is the normal case — and a drain that looked only for the `result`
                # threw it away: no row, no outbox row, no delivery, the text surviving only in
                # `session_frames` where nobody looks (<../debug/message_drops.md> E3). A message
                # the agent finished before it stopped is a message, so it is folded in by the one
                # piece of code that knows what folding one in means.
                #
                # It therefore counts towards `spoke` and `saw_assistant_message` exactly as it
                # would have a moment earlier, which is what keeps the tail below honest: the room
                # is not owed the turn's final text as well (it repeats that message), and no
                # second row is minted for it — leaving `ABORTED_NOTICE` to be said on its own, as
                # the one `turn_id`-keyed row this turn writes.
                #
                # The stream stays open for the next turn: it is the session's, so an interrupt
                # ends a turn rather than the conversation.
                received = await next_frame
                frame_seq = received.frame_seq
                # Claude's frame, not an event: `room_status` is the fourth of stage 4's four
                # interpreters and still reads the wire itself. Re-pointing it is its own change,
                # and until then it is the one thing here that a second backend would go quiet on.
                status.note(received.payload)
                for event in _projected(received):
                    match event:
                        case TextDelta():
                            if assistant_id is None:
                                assistant_id = await self._store.begin_assistant(
                                    session_id, turn_id, source_first_frame_seq=frame_seq
                                )
                            streamed += event.text
                            await self._store.update_assistant(
                                session_id, assistant_id, streamed, source_last_frame_seq=frame_seq
                            )
                            # The rollout keeps no deltas, so without this the text an interrupted
                            # turn produced would exist only in the message row and the log would
                            # simply stop mid-answer (R5.5b).
                            await self._store.update_partial_frame(session_id, streamed)
                        case ToolCallStarted():
                            tool_calls.append(
                                RecordedToolCall(
                                    call_id=event.call_id, tool_name=event.tool_name, arguments=dict(event.arguments)
                                )
                            )
                        case MessageCompleted():
                            saw_assistant_message = True
                            if assistant_id is None:
                                assistant_id = await self._store.begin_assistant(
                                    session_id, turn_id, source_first_frame_seq=frame_seq
                                )
                            said = (event.text or "").strip() or streamed.strip()
                            # Each message is queued for the room as it finishes rather than only
                            # the final answer (R11.1), so a turn that narrates, works and reports
                            # back is three messages in the room and not just its conclusion — and
                            # the row is written here, with the message, rather than after the turn
                            # has decided it is over.
                            spoke = (
                                await self._store.update_assistant(
                                    session_id,
                                    assistant_id,
                                    said,
                                    tool_calls=tool_calls,
                                    # Provenance, not identity: it is what the frames called this
                                    # message, and it is what lets a reader find its calls in the
                                    # log rather than match by position. Absent on thousands of
                                    # production rows, which is why nothing keys on it.
                                    agent_message_id=event.agent_message_id,
                                    source_last_frame_seq=frame_seq,
                                    complete=True,
                                )
                                or spoke
                            )
                            # The real frame is already in the log — the recorder wrote it when
                            # the socket delivered it — so the stand-in has nothing to stand for.
                            await self._store.clear_partial_frame(session_id)
                            assistant_id, streamed, tool_calls = None, "", []
                        case TurnCompleted():
                            completed = event
                            result, result_frame_seq = received.payload, frame_seq
                        case Reasoning() | ToolCallCompleted() | ActivityStarted() | ActivityCompleted():
                            # Projected and deliberately not stored: what the agent thought, what
                            # its calls answered and what the harness narrated are richer than any
                            # surface renders today, and giving them rows is the half of stage 4
                            # that moves data. Dropping them here changes nothing — the turn loop
                            # never saw these frames at all.
                            pass
            if result is None:
                raise RuntimeError("the Claude stream ended without a result for this turn")
            if completed.outcome is TurnOutcome.FAILED and not abort_event.is_set():
                # Quoted from the frame rather than the event: *why* a turn failed is
                # provider-specific by nature, and the neutral vocabulary carries an outcome
                # rather than a message on purpose.
                raise RuntimeError(
                    f"the agent's turn failed: {result.get('subtype')}: {result.get('stop_reason') or 'unknown error'}"
                )
            # `result.result` is deliberately not projected — it repeats the turn's last message on
            # every result frame, so minting prose from it would double every answer. It is still
            # the fallback for the one case that is not a repeat: a turn whose text arrived nowhere
            # else.
            final_text = streamed.strip() or str(result.get("result") or "").strip()
            if abort_event.is_set():
                final_text += f"\n\n{ABORTED_NOTICE}"
            if assistant_id is not None:
                # A stream no completed frame closed. Its `partial` frame stays exactly as the
                # last delta left it: the rollout should show a turn that stopped mid-answer as
                # having stopped mid-answer. `final_text` is not written over it, because the
                # harness adds `[aborted by operator]` to that and the frame records what the
                # agent produced, not what the room was told.
                # No frame range is passed: the deltas that produced this text already recorded
                # theirs, and the `result` frame closing the turn is not where the words came from.
                carried_final = await self._store.update_assistant(
                    session_id, assistant_id, final_text, tool_calls=[], complete=True
                )
                assistant_id = None
            elif not saw_assistant_message:
                # This row's only source is the `result` frame — the turn said nothing else.
                assistant_id = await self._store.begin_assistant(
                    session_id, turn_id, source_first_frame_seq=result_frame_seq
                )
                carried_final = await self._store.update_assistant(
                    session_id,
                    assistant_id,
                    final_text,
                    tool_calls=[],
                    source_last_frame_seq=result_frame_seq,
                    complete=True,
                )
                assistant_id = None
            else:
                # Every completed message queued its own row and one of them closed the answer, so
                # `final_text` — which is `result.result` repeating the last of them — belongs to
                # no row of its own.
                carried_final = False
            spoke = carried_final or spoke
            # Only what the room is not already owed. Each assistant message queued its own row as
            # it finished, and `result.result` normally repeats the last of them — so queueing
            # `final_text` unconditionally would post the answer twice. Two cases still need it: a
            # turn whose text belongs to no completed message at all, and an abort, whose notice
            # rides on `final_text` and therefore on no message row — which is exactly when a
            # message row did *not* just carry `final_text` into the outbox.
            #
            # **Before the turn is closed, not after.** Closing it is what makes it unadoptable, so
            # a replica dying between the two would strand this reply with nothing left to
            # re-derive it. This way the window leaves the turn open, and what the replacement
            # re-derives collides with the row already there (`session_outbox.turn_id`).
            if not spoke:
                await self._speak(session_id, room_id, turn_id, final_text)
            elif abort_event.is_set() and not carried_final:
                await self._speak(session_id, room_id, turn_id, ABORTED_NOTICE)
            # The outcome is the event's; the payload beside it is still Claude's, because
            # `session_turns` stores that CLI's cost, usage and duration as it arrived.
            # `TurnCompleted.usage` is the neutral shape that replaces it, and giving those
            # columns their own meaning is a schema change this one does not make.
            await self._store.end_turn(
                turn_id, TurnOutcome.ABORTED if abort_event.is_set() else completed.outcome, result
            )
        except Exception as error:
            await self._store.end_turn(turn_id, TurnOutcome.FAILED)
            if assistant_id is not None:
                await self._store.fail(session_id, str(error), assistant_id)
            raise
        finally:
            # The event outlives the turn (it is the session's), so only this turn's waiter goes.
            aborted.cancel()
            # Every terminal path, failure included: a line still saying "running Bash" after
            # the turn died is the stuck-typing-indicator bug R6.1 calls out, in another form.
            await status.finish()

    async def _speak(self, session_id: UUID, room_id: str | None, turn_id: UUID, text: str) -> None:
        """Queue the turn's last word for the room, or report that it had none (R11.2).

        Only ever the end of a turn: everything a completed assistant message says is queued with
        the message itself, in one transaction. What is left over is text that belongs to no
        message row — an abort notice, or an answer that arrived only on the `result` frame.

        A session with no room needs nothing here; the SPA's client reads the message rows the
        turn already wrote. An empty body is not a silence token (R11.2): the room is told that
        the turn finished without saying anything, as a notice rather than as a reply, because it
        is the console reporting an outcome and not the agent talking.
        """
        if self._room_surface is None or room_id is None:
            return
        if not await self._store.enqueue_turn_reply(session_id, turn_id, text):
            await self._room_surface.report_silent_turn(room_id)

    async def aclose(self) -> None:
        # Called from the lifespan on the way down. Handing every held lease back here is the
        # guarantee the per-connection releases cannot be: a cancelled `handle_runner` may not
        # finish its own commit, but this one statement does, so a graceful roll leaves no session
        # waiting out the sweep. Reachable only because `uvicorn.run` bounds `timeout_graceful_
        # shutdown` (see app.main) — otherwise shutdown never gets here.
        released = await self._store.release_held_leases()
        if released:
            logger.info("Released %d held session lease(s) on shutdown", released)
        await self._claims.aclose()


def _projected(received: ReceivedFrame) -> tuple[ConversationEvent, ...]:
    """What one frame means, in the vocabulary every surface and every backend shares.

    **One frame at a time, and a fresh projection for each**, which is what keeps this a change to
    how the turn loop decides rather than to what it stores. A projector held across the turn would
    merge the frames sharing one `message.id` into a single row and defer every completion to the
    frame after it — real improvements, both, and both changes to the transcript. The fold with a
    durable cursor beside it is the other half of stage 4.

    `Projection.unprojected` is dropped rather than logged: counting the classes this release has
    no meaning for is worth doing where the events are *stored*, and per frame in the hot path it
    would be a log line for every heartbeat.

    A frame the client never numbered — `ClaudeCli` with no rollout sink, which is tests and
    nothing in the console — still projects to the same events. Only its `FrameRange` has nothing
    real to point at, so the loop writes the sequence it holds instead of reading one back out;
    `FrameRange` is two integers on purpose, since "no frames at all" is `Authored` rather than a
    null range.
    """
    return claude_projection.project(
        [
            claude_projection.RecordedFrame(
                frame_seq=received.frame_seq if received.frame_seq is not None else _UNNUMBERED_FRAME,
                payload=received.payload,
            )
        ],
        delta_source=claude_projection.DeltaSource.STREAM_EVENTS,
    ).events


def _service(request: Request) -> SessionService:
    service = cast(SessionService | None, request.app.state.session_service)
    if service is None:
        raise HTTPException(status_code=503, detail="the session runtime is not configured")
    return service


def _store(request: Request) -> SessionStore:
    store = cast(SessionStore | None, request.app.state.session_store)
    if store is None:
        raise HTTPException(status_code=503, detail="the session runtime is not configured")
    return store


def _notifications(request: Request) -> SessionNotifications:
    notifications = cast(SessionNotifications | None, request.app.state.session_notifications)
    if notifications is None:
        raise HTTPException(status_code=503, detail="the session runtime is not configured")
    return notifications


SessionNotificationsDep = Annotated[SessionNotifications, Depends(_notifications)]
SessionServiceDep = Annotated[SessionService, Depends(_service)]
SessionStoreDep = Annotated[SessionStore, Depends(_store)]


@router.get("/api/conversations")
async def list_conversations(
    actor: OperatorActorDep, store: SessionStoreDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[ConversationSessionSummary]:
    return await store.list_operator_conversations(actor.operator_id, limit=limit)


@router.get("/api/conversations/{session_id}")
async def get_conversation(
    session_id: UUID, actor: OperatorActorDep, store: SessionStoreDep
) -> ConversationSessionView:
    try:
        return await store.get_operator_conversation(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error


@router.get("/api/conversations/{session_id}/frames")
async def read_conversation_frames(
    session_id: UUID,
    actor: OperatorActorDep,
    store: SessionStoreDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_FRAME_PAGE)] = DEFAULT_FRAME_PAGE,
    kind: Annotated[list[str] | None, Query()] = None,
) -> SessionFramePage:
    """The raw protocol frames behind a conversation, newest page first.

    What `session_messages` is a lossy projection *of*: the transcript this console shows is an
    interpretation, and this is the record it was interpreted from. Omitting `before_seq` opens on
    the end of the log; the response's `next_before_seq` walks back from there.

    `kind` is repeatable and open, because the column is: the CLI may send a `type` this release
    has never heard of, and an inspector that could only name a closed list would be the surface
    hiding exactly the frame worth looking at. Omitting it means everything except `stream_event`
    — see `session_store._frames_of_kinds`.
    """
    try:
        return await store.read_operator_frames(
            actor.operator_id, session_id, before_seq=before_seq, limit=limit, kinds=kind
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error


# The operator surface moved from `/api/claude/sessions` to `/api/sessions` with the tables it
# reads. Both are served, and only the new path is in the schema — so the generated client (and
# so the SPA) can only call the new one, while a browser tab that loaded the previous bundle keeps
# working until it reloads. That window is what `maxUnavailable: 0` makes unavoidable: old pods,
# and old tabs, outlive the release.
#
# CLEANUP(added 2026-08-15): drop the `include_in_schema=False` registrations one release after
# this ships, when no deployed bundle names them. `/internal/claude/runner` is *not* part of this
# rename — the runner image dials it and that is a two-sided roll of its own.
@router.post("/api/sessions")
@router.post("/api/claude/sessions", include_in_schema=False)
async def create_session(actor: OperatorActorDep, service: SessionServiceDep) -> SessionView:
    try:
        return await service.create(actor.operator_id, SpaSession())
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/api/sessions/{session_id}")
@router.get("/api/claude/sessions/{session_id}", include_in_schema=False)
async def get_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> SessionView:
    try:
        return await service.get(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error


async def _sse_stream(
    store: SessionStore, notifications: SessionNotifications, operator_id: UUID, session_id: UUID
) -> collections.abc.AsyncIterator[str]:
    """Server-Sent Events stream delivering real-time session updates via LISTEN/NOTIFY."""
    yield f"data: {json.dumps({'type': 'connected'})}\n\n"
    try:
        last_view = await store.get(operator_id, session_id)
    except KeyError:
        yield f"data: {json.dumps({'type': 'end'})}\n\n"
        return
    last_status, last_payload = last_view.status, last_view.model_dump_json()
    yield f"data: {last_payload}\n\n"
    while True:
        if last_status in {SessionStatus.CLOSED, SessionStatus.FAILED}:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        await notifications.wait(SessionEventKind.UPDATE, session_id, timeout_seconds=30.0)
        try:
            next_view = await store.get(operator_id, session_id)
        except KeyError:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        # Serialized once and compared against what was last sent, rather than three times per
        # wake: the view embeds the whole transcript, so each of those was the entire
        # conversation. It suppresses little during a turn — every delta really does change the
        # view — which is the reason not to pay for the comparison twice more.
        if (payload := next_view.model_dump_json()) != last_payload:
            last_status, last_payload = next_view.status, payload
            yield f"data: {payload}\n\n"


@router.get("/api/sessions/{session_id}/stream")
@router.get("/api/claude/sessions/{session_id}/stream", include_in_schema=False)
async def stream_session(
    session_id: UUID, actor: OperatorActorDep, store: SessionStoreDep, notifications: SessionNotificationsDep
) -> StreamingResponse:
    if not await store.session_exists(actor.operator_id, session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return StreamingResponse(
        _sse_stream(store, notifications, actor.operator_id, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/sessions/{session_id}/abort", status_code=202)
@router.post("/api/claude/sessions/{session_id}/abort", status_code=202, include_in_schema=False)
async def abort_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> dict[str, str]:
    try:
        aborted = await service.request_abort(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    if not aborted:
        raise HTTPException(status_code=409, detail="no active turn to abort")
    return {"status": "aborted"}


@router.post("/api/sessions/{session_id}/messages")
@router.post("/api/claude/sessions/{session_id}/messages", include_in_schema=False)
async def send_message(
    session_id: UUID, body: SessionPromptRequest, actor: OperatorActorDep, store: SessionStoreDep
) -> SessionMessageView:
    try:
        return await store.enqueue_prompt(actor.operator_id, session_id, body.text)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/api/sessions/{session_id}", status_code=204)
@router.delete("/api/claude/sessions/{session_id}", status_code=204, include_in_schema=False)
async def delete_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> None:
    try:
        await service.dispose(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error


@internal_router.websocket("/internal/claude/runner/{session_id}")
async def runner_websocket(websocket: WebSocket, session_id: UUID) -> None:
    service = cast(SessionService | None, websocket.app.state.session_service)
    authorization = websocket.headers.get("authorization", "")
    scheme, _, bearer = authorization.partition(" ")
    if service is None or scheme.lower() != "bearer" or not bearer:
        await websocket.close(code=NOT_ADMITTED_CODE, reason="runner authentication required")
        return
    await service.handle_runner(websocket, session_id, bearer)
