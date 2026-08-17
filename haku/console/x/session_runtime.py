"""Operator chat sessions backed by Claude Code in Agent Sandbox pods.

The service half: the turn loop, the runner's websocket bridge, the sandbox lifecycle and the SPA
chat surface's own routes. The rows underneath it, and every transaction that moves them, are
`session_store.py`.

The incidents behind this file's invariants are in <../debug/2026_08_16_runtime_archaeology.md>.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from more_itertools import first
from pydantic import BaseModel, Field, SecretStr

from haku.console.chat_models import ENDED_SESSION_STATUSES, FrameDirection, SessionStatus, TurnOutcome
from haku.console.config import ClaudeRuntimeConfig
from haku.console.operator_auth import OperatorActorDep
from haku.console.x import frame_projection
from haku.console.x.claude_code.frames import frame_kind
from haku.console.x.conversation_events import TurnCompleted
from haku.console.x.room_status import StatusFrontend, TurnStatus
from haku.console.x.sandbox_claims import ProvisioningStep, SandboxClaims, provisioning_view
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
from haku.console.x.setup_output import SETUP_OUTPUT_KIND, setup_output_frame
from haku.runtime.x.bridge.cli_client import ClaudeCli, ReceivedFrame, RecordedFrame, SentPrompt, cli_over_websocket
from haku.runtime.x.bridge.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runtime.x.bridge.protocol import GOING_AWAY_CODE, NOT_ADMITTED_CODE, TextWebSocket

router = APIRouter(tags=["sessions"])
internal_router = APIRouter(tags=["claude-chat-internal"])
logger = logging.getLogger(__name__)

# Appended to a turn's stored answer when the operator stopped it, and sent on its own when the
# room has already heard the turn's prose — so an abort is visible either way.
ABORTED_NOTICE = "[aborted by operator]"


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
    """What the SPA posts to send a prompt; the prompt itself is a row (`SessionPrompt`)."""

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

    **No exclusions.** Control frames, because an interrupt that did not take is diagnosable from
    nothing else; deltas, because a log with a hole in it cannot be folded over
    (<../../plans/chat_runtime_projection.md>). "Do not bury the reader" is answered at the read
    instead: `read_frames` leaves deltas out of its default view.
    """

    def __init__(self, store: SessionStore, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, payload: dict[str, Any]) -> int:
        # No `runner_seq`: the runner numbers what *it* puts on the wire, and this is a write to
        # the CLI that it only forwards.
        return (await self._record(FrameDirection.TO_AGENT, payload)).frame_seq

    async def received(self, payload: dict[str, Any], *, runner_seq: int | None) -> RecordedFrame:
        """Record the frame, answering whether the caller should act on it and where it landed.

        A delta has no agent-assigned identity, so it is always recorded and always fresh — safe
        because the runner never replays one (`runner.DELTA_TYPE`).

        *runner_seq* is kept beside the row's own `frame_seq` and read back as the session's resume
        cursor. Nothing orders by it yet.
        """
        return await self._record(FrameDirection.FROM_AGENT, payload, runner_seq=runner_seq)

    async def _record(
        self, direction: FrameDirection, payload: dict[str, Any], *, runner_seq: int | None = None
    ) -> RecordedFrame:
        return await self._store.record_frame(
            self._session_id, direction, frame_kind(payload), payload, runner_seq=runner_seq
        )


class ChatFrontend(StatusFrontend, Protocol):
    """The chat channel a session is attached to, for the parts a turn cannot do itself.

    **Bound to its address at construction**, never asked for one per call: a channel serves one
    room and a session serves one channel, so an address parameter on every method would be this
    loop re-asking what is answered once per connection. The three methods a running turn's
    status line and typing indicator need are `StatusFrontend`, declared beside the driver that
    calls them (<room_status.py>).

    The SPA needs none of this today — its client reads the message rows over SSE, so a finished
    turn is delivered by being written down. A room has to be spoken to.

    **The service picks this by reading the session's `surface`**, rather than offering every
    session to every listener and having each re-derive whether it is its own.

    **Replies are not here.** They are rows in `session_outbox`, written where they are produced
    and drained into the room by whoever holds the outbox lock (<../debug/message_drops.md>). What
    is left here is what describes a moment and is worthless afterwards.
    """

    async def system_prompt(self, session_id: UUID) -> str: ...

    async def report_silent_turn(self) -> None: ...

    async def report(self, detail: str) -> None: ...


@dataclass(frozen=True)
class _CompletedTurn:
    """The event that ended a turn, and the frame it was projected from."""

    event: TurnCompleted
    # Still read for the two things the neutral event does not carry: the failure's reason (an
    # outcome is not a message) and the prose of a turn that said nothing anywhere else. Appealing
    # an event to the frame behind it is the design's own escape hatch, so this is the seam working
    # rather than leaking. The turn's cost is not among them — that is `event.usage`, in columns
    # that mean the same thing whichever backend filled them.
    frame: ReceivedFrame


class SessionService:
    def __init__(
        self,
        config: ClaudeRuntimeConfig,
        store: SessionStore,
        claims: SandboxClaims,
        notifications: SessionNotifications,
        *,
        mcp_token: SecretStr,
        chat_frontend: ChatFrontend | None = None,
    ):
        self._config = config
        self._store = store
        self._claims = claims
        self._notifications = notifications
        self._mcp_token = mcp_token
        self._chat_frontend = chat_frontend

    async def request_abort(self, operator_id: UUID, session_id: UUID) -> bool:
        """Interrupt this session's turn, or answer False when it has none.

        Raises `KeyError` for a session this Operator does not own, so the route asks one question
        rather than reaching through `service._store` for an ownership check first.
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
            # resource now. A failed delete leaves `claim_cleaned_at` NULL, which is the durable
            # retry marker.
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
            # Leave `claim_cleaned_at` NULL so another replica or a later restart retries.
            # Kubernetes deletion is idempotent, so a redundant retry costs a 404.
            logger.warning("Claude claim cleanup failed for session %s: %s", session_id, error)
            return False
        await self._store.complete_claim_cleanup(session_id)
        return True

    async def _frontend_for(self, session_id: UUID) -> ChatFrontend | None:
        """The chat frontend this session is attached to, or None for one attached to none.

        The frontend is bound to its room, so what is asked here is which sessions it serves — the
        session's own `surface`, immutable on the row. Read once per runner connection and carried
        for the session's life, so re-reading it would only add round trips.
        """
        if self._chat_frontend is None:
            return None
        return self._chat_frontend if await self._store.room_of(session_id) is not None else None

    async def _appended_prompt(self, session_id: UUID, frontend: ChatFrontend | None) -> str | None:
        """Who this session is, appended to Claude Code's own system prompt.

        Appended, not replacing: the built-ins (Read, Bash, Edit) are live in the sandbox and the
        preset is what tells the model how to drive them. Hence `--append-system-prompt` and never
        `--system-prompt`.
        """
        return None if frontend is None else await frontend.system_prompt(session_id)

    def _progress_reporter(self, session_id: UUID, frontend: ChatFrontend | None) -> Callable[[str], Awaitable[None]]:
        """Record every sandbox progress report, log it, and show it to the frontend if there is one.

        Recorded first because the rollout is the only durable copy: the pod's log is reaped with
        the sandbox, and a session that died before its first CLI frame has its whole account here.
        """

        async def report(detail: str) -> None:
            logger.info("Claude sandbox %s: %s", session_id, detail)
            await self._store.record_frame(
                session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame(detail)
            )
            if frontend is not None:
                await frontend.report(detail)

        return report

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == BridgeAuthentication.HELD:
            # **A denial response, not a close.** uvicorn renders any pre-`accept()` close as
            # HTTP 403 whatever code is passed, and the runner gives up on a 4xx — correctly, since
            # a bad credential is not worth redialling. The ASGI `websocket.http.response`
            # extension is what lets this answer 503 instead, which `_worth_redialling` retries
            # along with every other 5xx, that being what the Gateway says mid-roll.
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
        # outlived it, so the rest of that exchange is about to arrive on this socket. What that
        # holder recorded and did not get to project comes back with the turn, to be fed to the
        # loop ahead of the live stream.
        #
        # **Read before the socket is accepted**, which is what stops a frame being both replayed
        # here and delivered fresh: `RolloutRecorder.received` records a frame at the moment
        # `ClaudeCli._read` routes it, and nothing is being read on this connection yet.
        resumed = await self._store.adopt_open_turn(session_id)
        if resumed is not None:
            logger.warning(
                "session %s adopted with turn %s still running; re-projecting %d recorded frame(s)",
                session_id,
                resumed.turn_id,
                len(resumed.replay),
            )
        # Rendered before the socket is accepted, with the other admission failures, so a broken
        # prompt ends the session where the supervisor can see it (and say so in the room) rather
        # than raising past the cleanup below and stranding the claim. Failing is deliberate: a
        # session that silently started without its identity is the generic-assistant bug this
        # prompt exists to fix, and it would be invisible.
        try:
            frontend = await self._frontend_for(session_id)
            appended = await self._appended_prompt(session_id, frontend)
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
            # The cursor is read here, per connection, off the session's own rows — so a replica
            # adopting a session mid-turn asks for what it is missing rather than being handed the
            # runner's whole replay window (<../../plans/chat_runtime_projection.md> § 2b).
            build_claude_launch(session, resume_from=await self._store.highest_runner_seq(session_id)),
            self._progress_reporter(session_id, frontend),
            RolloutRecorder(self._store, session_id),
        )
        abort_event = asyncio.Event()
        # Whether the sandbox should outlive this connection. False for an ending session — one
        # closed, or failed in a way the CLI cannot be asked to continue past — and true when it
        # is only this replica that is going away.
        keep_sandbox = False
        # Two nested handlers because Python forbids `except` and `except*` on one `try`, and they
        # are about different things: the inner unwraps what the task group failed with, the outer
        # is this whole activity being cancelled.
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
                        # One stream for the session, not one per turn: a folded prompt is answered
                        # with no second `result`, and an adopted turn was issued by a process that
                        # is gone. A turn is a bracket over this stream, not a request/response pair
                        # (<../../plans/cli_protocol_ownership.md>).
                        frames = _replaying(() if resumed is None else resumed.replay, client.frames().__aiter__())
                        while True:
                            status = await self._store.status(session_id)
                            if status is None or status in ENDED_SESSION_STATUSES:
                                break
                            # The inherited turn before any new prompt, and once: its remaining
                            # frames are already on their way, so opening a second turn to take them
                            # would deliver one exchange's answer into another's bracket.
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
                                    client, frames, session_id, turn, frontend=frontend, abort_event=abort_event
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
                # The runner went away, which is not the session being over: it keeps the CLI alive
                # across a lost socket and redials. Hand the session back and let the lease decide —
                # a runner that never returns leaves the row to the sweep.
                logger.info("session %s lost its runner; leaving it for adoption", session_id)
                keep_sandbox = True
                await self._store.release_lease(session_id)
            except* Exception as errors:
                # `fail` records the message; the traceback is what says which call produced it.
                logger.exception("Claude runtime failed for session %s", session_id)
                await self._store.fail(session_id, f"Claude runtime failed: {_first_message(errors)}")
        except asyncio.CancelledError:
            # A `BaseException`, so neither clause above sees it. This is the replica going away —
            # a rolling update, an evicted pod — which says nothing about the session, so it must
            # not be recorded as a failure: a terminal row refuses the runner's reconnect and the
            # supervisor builds a replacement. Hand it back instead; the sandbox outlives this
            # process and whichever replica the runner redials adopts it. Nothing is swallowed —
            # the sweep fails the session once its adoption window passes with no runner back.
            keep_sandbox = True
            await self._store.release_lease(session_id)
            raise
        finally:
            # Shielded because everything here is an `await` and this task may already be
            # cancelled, in which case the first would re-raise and the rest silently not happen.
            # Best effort even so — a SIGKILL runs no finalizer, which is why the lease and not
            # this block is what guarantees the session stops looking alive.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.shield(
                    asyncio.wait_for(self._finalize(session_id, websocket, client, keep_sandbox), timeout=10)
                )

    async def _finalize(self, session_id: UUID, websocket: WebSocket, client: ClaudeCli, keep_sandbox: bool) -> None:
        """Let go of one runner connection, and of the session itself unless it outlives us.

        `keep_sandbox` is the difference between "this conversation is over" and "this replica is".
        Never delete the claim on the second: the sandbox is what the adopting replica reconnects
        to.
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
        renewed lease rather than a `session_ttl_seconds` hard timer (`sandbox_claims.renew`).
        Console lease and sandbox deadline lapse together the moment a replica stops tending it.
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

        The reader is a detached task, so a dropped socket cannot propagate into the task group by
        itself — it becomes a `None` sentinel only a *turn* consumer sees. An idle session is not
        consuming, so without this it sits in the prompt-wait until graceful shutdown cancels it.
        Waking here routes the drop to the `except* WebSocketDisconnect` clause, so a roll hands the
        session back at once instead of after that timeout.
        """
        await client.wait_closed()
        raise WebSocketDisconnect(code=GOING_AWAY_CODE)

    async def _watch_aborts(self, session_id: UUID, abort_event: asyncio.Event) -> None:
        """Set *abort_event* every time this session is told to abort, until cancelled.

        The operator's abort lands on whichever replica the Service picks, rarely the one holding
        this session's websocket, so it arrives over NOTIFY rather than by a caller reaching into
        this process.
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
        frontend: ChatFrontend | None,
        abort_event: asyncio.Event,
    ) -> None:
        """Ask *turn*'s question if it has not been asked, then consume the stream until the turn
        completes.

        **Project, then act.** Every frame goes through `claude_code.projection` and this loop acts on
        the neutral events that come back, so what it knows about is prose, messages, tool calls
        and a completed turn — not `assistant`, `stream_event` and `result`
        (<../../plans/chat_runtime_projection.md> § stage 4).

        *frames* belongs to the session, not to this call — see `handle_runner`. This call is the
        turn's span and the only thing that closes it, so a turn left open is not a bookkeeping leak:
        it means no code got to close it, which is what a replica losing its pod mid-exchange looks
        like from outside, and what `ResumedTurn` picks back up.

        **No turn state is held here.** `state` is the row's, re-read from every write of it, and
        each frame's effects are written with the projection cursor in one transaction
        (`SessionStore.apply_frame`) — so a process dying anywhere in this loop leaves
        `session_turns` saying what had happened and the session saying which frame it had got
        through, which is what makes adoption a read and its effects exactly-once
        (<../../plans/chat_runtime_projection.md> § The shape).
        """
        turn_id = turn.turn_id
        if isinstance(turn, TurnStart):
            # A resumed turn's question was asked by a process that is gone; only its answer is
            # still coming.
            prompt = await client.query(turn.prompt)
            # The prompt's row was written when the operator typed it, before any frame existed to
            # point at; this is where the question acquires the frame it went out as.
            await self._store.set_message_source_frames(session_id, turn.message_id, prompt.frame_seq)
        # How far the turn has got: the message it is streaming into and what is in it,
        # `said_anything`, and `queued_reply` — the outbox row's existence, recorded on the turn by
        # the transaction that inserts it, never a report from the delivery layer and never
        # `sent_at`, which is the drain's business and comes later. The two booleans are separate
        # facts because a session with no room queues nothing.
        state = await self._store.turn_state(turn_id)
        assistant_id = state.assistant_message_id
        completed: _CompletedTurn | None = None
        status = TurnStatus(frontend)
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
                # the `result` is the normal case, and it is applied like any other — a message the
                # agent finished before it stopped is a message (<../debug/message_drops.md> E3).
                #
                # It therefore moves `said_anything` and `queued_reply` exactly as it would have a
                # moment earlier, which is what keeps the tail below honest: the room is not owed
                # the turn's final text as well (it repeats that message), and no second row is
                # minted for it — leaving `ABORTED_NOTICE` to be said on its own, as the one
                # `turn_id`-keyed row this turn writes.
                #
                # The stream stays open for the next turn: it is the session's, so an interrupt
                # ends a turn rather than the conversation.
                received = await next_frame
                frame_seq = received.frame_seq
                events = frame_projection.projected(frame_seq=frame_seq, payload=received.payload)
                # One frame's worth at a time, which is the granularity `coarse_status` reads a run
                # of events at: a tool call starting and its message completing arrive together.
                status.note(events)
                # The frame that ends the turn goes no further: what is left of the exchange is
                # written below and `end_turn` is the transaction that closes it and carries the
                # cursor past this frame, so projecting it into `apply_frame` would advance the
                # cursor ahead of the turn's own last word.
                finished = first((event for event in events if isinstance(event, TurnCompleted)), None)
                if finished is not None:
                    completed = _CompletedTurn(finished, received)
                else:
                    state = await self._store.apply_frame(session_id, turn_id, frame_seq, events)
                    assistant_id = state.assistant_message_id
            result = completed.frame.payload
            if completed.event.outcome is TurnOutcome.FAILED and not abort_event.is_set():
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
            final_text = state.streamed.strip() or str(result.get("result") or "").strip()
            if abort_event.is_set():
                final_text += f"\n\n{ABORTED_NOTICE}"
            if assistant_id is not None:
                # A stream no completed frame closed.
                # No frame range is passed: the deltas that produced this text already recorded
                # theirs, and the `result` frame closing the turn is not where the words came from.
                carried_final = await self._store.update_assistant(
                    session_id, assistant_id, final_text, tool_calls=[], complete=True
                )
                assistant_id = None
            elif not state.said_anything:
                # This row's only source is the `result` frame — the turn said nothing else.
                assistant_id = await self._store.begin_assistant(
                    session_id, turn_id, source_first_frame_seq=completed.frame.frame_seq
                )
                carried_final = await self._store.update_assistant(
                    session_id,
                    assistant_id,
                    final_text,
                    tool_calls=[],
                    source_last_frame_seq=completed.frame.frame_seq,
                    complete=True,
                )
                assistant_id = None
            else:
                # Every completed message queued its own row and one of them closed the answer, so
                # `final_text` — which is `result.result` repeating the last of them — belongs to
                # no row of its own.
                carried_final = False
            spoke = carried_final or state.queued_reply
            # Only what the room is not already owed. Each assistant message queued its own row as
            # it finished and `result.result` normally repeats the last of them, so queueing
            # `final_text` unconditionally would post the answer twice. Two cases still need it: a
            # turn whose text belongs to no completed message, and an abort, whose notice rides on
            # `final_text` and therefore on no message row.
            #
            # **Before the turn is closed, not after.** Closing it makes it unadoptable, so a
            # replica dying between the two would strand this reply with nothing left to re-derive
            # it. This way the window leaves the turn open, and what the replacement re-derives
            # collides with the row already there (`session_outbox.turn_id`).
            if not spoke:
                await self._speak(session_id, frontend, turn_id, final_text)
            elif abort_event.is_set() and not carried_final:
                await self._speak(session_id, frontend, turn_id, ABORTED_NOTICE)
            # Both halves are the event's: the outcome, and the usage the backend's adapter read
            # out of its own payload. Nothing about a turn's cost passes through here as a CLI's
            # frame any more.
            await self._store.end_turn(
                turn_id,
                TurnOutcome.ABORTED if abort_event.is_set() else completed.event.outcome,
                completed.event.usage,
                # One frame, said twice because it means two things here: where this turn's frames
                # end, and that this transaction is the one taking the cursor past it.
                last_frame_seq=completed.frame.frame_seq,
                projected_frame_seq=completed.frame.frame_seq,
            )
        except Exception as error:
            # Bounded only where the failure was diagnosed from the `result` frame; otherwise this
            # turn ended on no frame of its own and `end_turn` bounds it by what it recorded.
            await self._store.end_turn(
                turn_id, TurnOutcome.FAILED, last_frame_seq=completed.frame.frame_seq if completed is not None else None
            )
            if assistant_id is not None:
                await self._store.fail(session_id, str(error), assistant_id)
            raise
        finally:
            # The event outlives the turn (it is the session's), so only this turn's waiter goes.
            aborted.cancel()
            # Every terminal path, failure included: a line still saying "running Bash" after
            # the turn died is the stuck-typing-indicator bug R6.1 calls out, in another form.
            await status.finish()

    async def _speak(self, session_id: UUID, frontend: ChatFrontend | None, turn_id: UUID, text: str) -> None:
        """Queue the turn's last word for the room, or report that it had none (R11.2).

        Only ever the end of a turn: a completed assistant message queues its own copy in the same
        transaction. What is left over belongs to no message row — an abort notice, or an answer
        that arrived only on the `result` frame.

        A session attached to no frontend needs nothing here; the SPA reads the message rows the
        turn already wrote. An empty body is not a silence token (R11.2): the room is told the turn
        said nothing, as a notice rather than a reply, because it is the console reporting and not
        the agent talking.
        """
        if frontend is None:
            return
        if not await self._store.enqueue_turn_reply(session_id, turn_id, text):
            await frontend.report_silent_turn()

    async def aclose(self) -> None:
        # Called from the lifespan on the way down. Handing every held lease back here is the
        # guarantee the per-connection releases cannot be: a cancelled `handle_runner` may not
        # finish its own commit, this one statement does, so a graceful roll leaves no session
        # waiting out the sweep. Reachable only because `uvicorn.run` bounds
        # `timeout_graceful_shutdown` (see app.main).
        released = await self._store.release_held_leases()
        if released:
            logger.info("Released %d held session lease(s) on shutdown", released)
        await self._claims.aclose()


async def _replaying(
    recorded: Sequence[ReceivedFrame], live: AsyncIterator[ReceivedFrame]
) -> AsyncIterator[ReceivedFrame]:
    """The frames past the session's cursor, then the ones still to arrive.

    **This is what makes adoption and steady state one call.** The turn loop consumes one iterator
    and cannot tell which half a frame came from, so "project each frame as it lands" and "project
    from the stored cursor, which happens to be behind" are the same code with a different starting
    cursor (<../../plans/chat_runtime_projection.md> § The shape). A turn whose ending is among the
    recorded frames therefore closes without the socket being consulted, which is what used to be a
    separate question asked of the log.
    """
    for frame in recorded:
        yield frame
    async for frame in live:
        yield frame


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

    What `session_messages` is a lossy projection *of*. Omitting `before_seq` opens on the end of
    the log; the response's `next_before_seq` walks back from there.

    `kind` is repeatable and open, because the column is: the CLI may send a `type` this release has
    never heard of, and an inspector limited to a closed list would hide exactly the frame worth
    looking at. Omitting it means everything except `stream_event` — see
    `session_store._frames_of_kinds`.
    """
    try:
        return await store.read_operator_frames(
            actor.operator_id, session_id, before_seq=before_seq, limit=limit, kinds=kind
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error


@router.post("/api/sessions")
async def create_session(actor: OperatorActorDep, service: SessionServiceDep) -> SessionView:
    try:
        return await service.create(actor.operator_id, SpaSession())
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/api/sessions/{session_id}")
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
        # Serialized once and compared against what was last sent: the view embeds the whole
        # transcript, so every serialization is the entire conversation. It suppresses little
        # during a turn — every delta really does change the view — hence not paying for it more
        # than once per wake.
        if (payload := next_view.model_dump_json()) != last_payload:
            last_status, last_payload = next_view.status, payload
            yield f"data: {payload}\n\n"


@router.get("/api/sessions/{session_id}/stream")
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
async def abort_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> dict[str, str]:
    try:
        aborted = await service.request_abort(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    if not aborted:
        raise HTTPException(status_code=409, detail="no active turn to abort")
    return {"status": "aborted"}


@router.post("/api/sessions/{session_id}/messages")
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
