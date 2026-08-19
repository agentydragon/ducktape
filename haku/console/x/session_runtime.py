"""Operator chat sessions backed by Claude Code in Agent Sandbox pods.

The turn loop, the runner's websocket bridge, the sandbox lifecycle and the SPA chat surface's own
routes. The rows underneath, and every transaction that moves them, are `session_store.py`.

The incidents behind this file's invariants are in <../debug/2026_08_16_runtime_archaeology.md>.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from more_itertools import first
from pydantic import BaseModel, Field, SecretStr

from haku.console.chat_models import ENDED_SESSION_STATUSES, SPA_ORIGIN, FrameDirection, SessionStatus, TurnOutcome
from haku.console.config import ClaudeRuntimeConfig
from haku.console.operator_auth import OperatorActorDep
from haku.console.x import frame_projection
from haku.console.x.claude_code.frames import frame_kind
from haku.console.x.conversation_events import OpenItem, ProjectionState, TurnCompleted
from haku.console.x.room_status import StatusFrontend, TurnStatus
from haku.console.x.sandbox_claims import (
    ClaudeSandboxProvisioningView,
    ProvisioningStep,
    SandboxClaims,
    provisioning_view,
)
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_store import (
    LEASE_RENEW_INTERVAL,
    BridgeAuthentication,
    PromptRefusedError,
    ResumedTurn,
    SessionStore,
    TurnStart,
)
from haku.console.x.session_views import (
    DEFAULT_FRAME_PAGE,
    MAX_FRAME_PAGE,
    ConversationCursor,
    ConversationPage,
    ConversationView,
    SessionFramePage,
    SessionProvisioningView,
    SessionView,
)
from haku.runtime.x.bridge.cli_client import ClaudeCli, ReceivedFrame, RecordedFrame, SentPrompt, cli_over_websocket
from haku.runtime.x.bridge.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runtime.x.bridge.protocol import GOING_AWAY_CODE, NOT_ADMITTED_CODE, TextWebSocket

router = APIRouter(tags=["sessions"])
internal_router = APIRouter(tags=["claude-chat-internal"])
logger = logging.getLogger(__name__)

# How long one session's observed provisioning state is reused before the cluster is read again.
# Bounds what a polling browser costs the Kubernetes API server.
OBSERVATION_TTL = timedelta(seconds=2)


def _first_message(errors: BaseExceptionGroup[Exception]) -> str:
    """The message of the first leaf in *errors*, for the operator-facing `error` column.

    `except*` hands back a group even for a single failure, and a group's own `str` is a count
    ("1 sub-exception").
    """
    leaves = errors.exceptions
    while leaves and isinstance(leaves[0], BaseExceptionGroup):
        leaves = leaves[0].exceptions
    return str(leaves[0]) if leaves else str(errors)


class SessionPromptRequest(BaseModel):
    """What the SPA posts to send a prompt; the prompt itself becomes an item and a queued row."""

    text: str = Field(min_length=1, max_length=100_000)


class PromptAccepted(BaseModel):
    """The prompt is an item on the conversation, and a turn will take it.

    The id alone: the item's own rows reach the composer over the conversation's follow socket,
    which is where every other surface's prompts arrive too, so answering with a copy of it here
    would be the same prose by two routes.
    """

    item_id: UUID


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

    No exclusions — control frames, because an interrupt that did not take is diagnosable from
    nothing else, and deltas, because a log with a hole in it cannot be folded over. Not burying
    the reader is the read's job: `read_frames` leaves deltas out of its default view.
    """

    def __init__(self, store: SessionStore, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, payload: dict[str, Any]) -> int:
        # No `runner_seq`: the runner numbers what it puts on the wire, and this it only forwards.
        return (await self._record(FrameDirection.TO_AGENT, payload)).frame_seq

    async def received(self, payload: dict[str, Any], *, runner_seq: int | None) -> RecordedFrame:
        """Record the frame, answering whether the caller should act on it and where it landed.

        A delta has no agent-assigned identity, so it is always recorded and always fresh — safe
        because the runner never replays one (`runner.DELTA_TYPE`).

        *runner_seq* is kept beside the row's own `frame_seq` and read back as the session's resume
        cursor. Nothing orders by it.
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
    room and a session serves one channel. The three methods a running turn's status line and
    typing indicator need are `StatusFrontend`, declared beside the driver that calls them
    (<room_status.py>).

    Which sessions it serves is whether a channel holds a copy of the conversation they run
    (`SessionStore.attached`). The SPA needs none of this — its client follows the conversation, so
    a finished turn is delivered by being written down. A room has to be spoken to.

    **Replies are not here.** They are rows in `session_outbox`, written where they are produced
    and drained into the room by whoever holds the outbox lock (<../debug/message_drops.md>).
    Neither is anything the stream already records: a channel subscribes to the conversation and
    renders what it reads from its own position (<subscription.py>). What is left is what no row
    carries — the turn that produced nothing to record, and the sandbox's setup narration.
    """

    async def system_prompt(self, session_id: UUID) -> str: ...

    async def report_silent_turn(self) -> None: ...

    async def report(self, detail: str) -> None: ...


@dataclass(frozen=True)
class _CompletedTurn:
    """The event that ended a turn, and the frame it was projected from."""

    event: TurnCompleted
    # Read for the two things the neutral event does not carry: the failure's reason (an outcome is
    # not a message) and the prose of a turn that said nothing anywhere else.
    frame: ReceivedFrame


def _inherited(turn: TurnStart | ResumedTurn) -> ProjectionState:
    """Where an adopting replica's fold picks the turn up.

    Empty for a turn this process opened, and for one whose predecessor had no message open. What
    the store hands back is the item, so the prose lands on the same row either way; what this
    carries is how much of it has been said, without which the completed block arriving next is
    stored on top of the half already there.
    """
    if isinstance(turn, TurnStart) or turn.streaming is None:
        return ProjectionState()
    return ProjectionState(
        open_message=OpenItem(
            opened_at_frame_seq=turn.streaming.first_frame_seq,
            last_frame_seq=turn.streaming.last_frame_seq,
            backend_item_id=None,
            delivered=turn.streaming.text,
        )
    )


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
        # Per session, the last view read off the cluster; `_observed` drops entries older than
        # `OBSERVATION_TTL` as it goes.
        self._observations: dict[UUID, ClaudeSandboxProvisioningView] = {}

    async def request_abort(self, operator_id: UUID, session_id: UUID) -> bool:
        """Interrupt this session's turn, or answer False when it has none.

        Raises `KeyError` for a session this Operator does not own.
        """
        if not await self._store.session_exists(operator_id, session_id):
            raise KeyError(session_id)
        return await self._store.request_abort(session_id)

    async def create(self, operator_id: UUID, *, conversation_id: UUID | None = None) -> SessionView:
        view, token = await self._store.create(operator_id, conversation_id=conversation_id)
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
        return view

    async def create_conversation(self, operator_id: UUID) -> ConversationView:
        """Open a thread and the session that runs it, and read the thread back."""
        view = await self.create(operator_id)
        return await self.conversation(operator_id, await self._store.conversation_of(view.session_id))

    async def conversation(self, operator_id: UUID, conversation_id: UUID) -> ConversationView:
        view = await self._store.get_operator_conversation(operator_id, conversation_id)
        sandbox = await self.provisioning_of(view.session.session_id, view.session.status)
        return view.model_copy(update={"session": view.session.model_copy(update={"provisioning": sandbox})})

    async def sandbox_provisioning(self, operator_id: UUID, session_id: UUID) -> SessionProvisioningView:
        """How this one session's sandbox came up — asked of a session in any state.

        **Per session, because a conversation runs several.** Each got its own sandbox and the one
        that died has its own account of why; the conversation read below carries this for the
        current session only.

        The answer is the live claim/Sandbox/Pod/runner graph in every state — `failed` included,
        which is the whole point of asking a non-provisioning session. Once cleanup deletes the
        claim the answer becomes `claim_absent`, which is truthful rather than an error.

        Raises `KeyError` for a session this Operator does not own.
        """
        return SessionProvisioningView(
            session_id=session_id,
            status=await self._store.operator_status(operator_id, session_id),
            sandbox=await self._observed(session_id),
        )

    async def provisioning_of(self, session_id: UUID, status: SessionStatus) -> ClaudeSandboxProvisioningView | None:
        """What Kubernetes says about a sandbox still coming up, for a session still waiting on one.

        Only while it is what the operator is waiting on, unlike `sandbox_provisioning`: this read
        goes out with every whole-conversation read and with every update a follower is sent, so
        asking for a session already past provisioning would put a cluster read on the transcript's
        hot path.

        **Not a fact about the conversation**, which is why it is read here and not in the store: it
        is an observation of another system, on that system's clock. Nothing in `session_events`
        moves when a pod goes ready, so whoever shows this has to ask again rather than wait to be
        told (`conversation_follow.SANDBOX_POLL`).
        """
        if status != SessionStatus.PROVISIONING:
            return None
        return await self._observed(session_id)

    async def _observed(self, session_id: UUID) -> ClaudeSandboxProvisioningView:
        """The cluster's account of one session's sandbox — never raising, never hammered.

        An unreachable Kubernetes comes back as `observation_error` on the view rather than as an
        exception replacing the whole answer (<sandbox_claims.py>), and a failure is remembered
        like a success, so an API server that is down is asked at the same bounded rate as one that
        is up. One session's view — up to three Kubernetes reads — is reused for `OBSERVATION_TTL`,
        and carries the `inspected_at` it was taken at.
        """
        now = datetime.now(UTC)
        self._observations = {
            observed: view for observed, view in self._observations.items() if now - view.inspected_at < OBSERVATION_TTL
        }
        if (fresh := self._observations.get(session_id)) is not None:
            return fresh
        try:
            view = await self._claims.inspect(session_id=session_id)
        except Exception as error:
            view = provisioning_view(
                f"claude-{session_id.hex}", step=ProvisioningStep.CLAIM_CREATED, observation_error=str(error)
            )
        self._observations[session_id] = view
        return view

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

        The frontend is bound to its room, so what is asked here is whether a channel holds a copy
        of the thread this session runs. Read once per runner connection and carried for the
        session's life.
        """
        if self._chat_frontend is None:
            return None
        return self._chat_frontend if await self._store.attached(session_id) else None

    async def _appended_prompt(self, session_id: UUID, frontend: ChatFrontend | None) -> str | None:
        """Who this session is, appended to Claude Code's own system prompt.

        `--append-system-prompt` and never `--system-prompt`: the built-ins (Read, Bash, Edit) are
        live in the sandbox and Claude Code's own preset is what tells the model how to drive them.
        """
        return None if frontend is None else await frontend.system_prompt(session_id)

    def _progress_reporter(self, session_id: UUID, frontend: ChatFrontend | None) -> Callable[[str], Awaitable[None]]:
        """Record every sandbox progress report, log it, and show it to the frontend if there is one.

        The rollout is the only durable copy: the pod's log is reaped with the sandbox, and a
        session that died before its first CLI frame has its whole account here.
        """

        async def report(detail: str) -> None:
            logger.info("Claude sandbox %s: %s", session_id, detail)
            await self._store.narrate(session_id, detail)
            if frontend is not None:
                await frontend.report(detail)

        return report

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == BridgeAuthentication.HELD:
            # **A denial response, not a close.** uvicorn renders any pre-`accept()` close as
            # HTTP 403 whatever code is passed, and the runner gives up on a 4xx. The ASGI
            # `websocket.http.response` extension is what lets this answer 503 instead, which
            # `_worth_redialling` retries along with every other 5xx.
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
        # The sandbox outlived the previous holder, so the rest of its exchange is about to arrive
        # on this socket; what it recorded and did not get to project comes back with the turn, to
        # be fed to the loop ahead of the live stream.
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
        # prompt ends the session where the supervisor can see it rather than raising past the
        # cleanup below and stranding the claim. Failing is deliberate: a session that silently
        # started without its identity is a generic assistant, and invisibly so.
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
            # runner's whole replay window (<README.md> § `session_store.py` and `session_runtime.py`).
            build_claude_launch(session, resume_from=await self._store.highest_runner_seq(session_id)),
            self._progress_reporter(session_id, frontend),
            RolloutRecorder(self._store, session_id),
        )
        abort_event = asyncio.Event()
        # Whether the sandbox should outlive this connection. False for an ending session — one
        # closed, or failed in a way the CLI cannot be asked to continue past — and true when it
        # is only this replica that is going away.
        keep_sandbox = False
        # Two nested handlers because Python forbids `except` and `except*` on one `try`.
        try:
            try:
                async with asyncio.TaskGroup() as helpers:
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
                        # is gone. A turn is a bracket over this stream, not a request/response
                        # pair.
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
            # a rolling update, an evicted pod — which says nothing about the session: recording it
            # as a failure gives the session a terminal row, which refuses the runner's reconnect.
            # Hand it back instead; the sandbox outlives this process and whichever replica the
            # runner redials adopts it. Nothing is swallowed — the sweep fails the session once its
            # adoption window passes with no runner back.
            keep_sandbox = True
            await self._store.release_lease(session_id)
            raise
        finally:
            # Shielded because everything here is an `await` and this task may already be
            # cancelled, in which case the first would re-raise and the rest silently not happen.
            # Best effort even so: a SIGKILL runs no finalizer, so the lease and not this block is
            # what guarantees the session stops looking alive.
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
        renewed lease rather than a `session_ttl_seconds` hard timer (`sandbox_claims.renew`), and
        both lapse together the moment a replica stops tending it.
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
        itself — it becomes a `None` sentinel only a *turn* consumer sees, and an idle session is
        not consuming. Waking here routes the drop to the `except* WebSocketDisconnect` clause, so
        a roll hands the session back at once instead of after the graceful-shutdown timeout.
        """
        await client.wait_closed()
        raise WebSocketDisconnect(code=GOING_AWAY_CODE)

    async def _watch_aborts(self, session_id: UUID, abort_event: asyncio.Event) -> None:
        """Set *abort_event* every time this session is told to abort, until cancelled.

        The operator's abort lands on whichever replica the Service picks, rarely the one holding
        this session's websocket, so it arrives over NOTIFY rather than in process.
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

        **Project, then act.** Every frame goes through `claude_code.projection` and this loop acts
        on the neutral events that come back, so what it knows about is prose, messages, tool calls
        and a completed turn — not `assistant`, `stream_event` and `result`
        (<README.md> § The neutral projection).

        *frames* belongs to the session, not to this call — see `handle_runner`. This call is the
        turn's span and the only thing that closes it, so a turn left open means no code got to
        close it, which is what a replica losing its pod mid-exchange looks like from outside and
        what `ResumedTurn` picks back up.

        **No turn state is held here at all.** How far the turn has got is derived from the items
        it opened, so the loop never carries a copy that could disagree; each frame's effects are
        written with the projection cursor in one transaction (`SessionStore.apply_frame`), so a
        process dying anywhere in this loop leaves the items saying what had happened and the
        session saying which frame it had got through — which is what makes adoption a read and its
        effects exactly-once (<README.md> § The cursor).
        """
        turn_id = turn.turn_id
        if isinstance(turn, TurnStart):
            # A resumed turn's question was asked by a process that is gone; only its answer is
            # still coming.
            await client.query(turn.prompt)
        # Threaded across the turn's frames rather than seeded per frame: a delta carries no
        # `message.id`, so an empty seed would make an item of every one of them
        # (`frame_projection`). A turn's own state, because a turn is what a message belongs to —
        # and an inherited turn starts from what its predecessor had already said, so the block
        # that finishes the answer is not stored on top of the half of it already there.
        folding = _inherited(turn)
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
                # the `result` is the normal case and is applied like any other
                # (<../debug/message_drops.md> E3). It therefore moves `said_anything` and
                # `queued_reply`, which is what keeps the tail below from owing the room the turn's
                # final text as well.
                #
                # The stream stays open for the next turn: it is the session's, so an interrupt
                # ends a turn rather than the conversation.
                received = await next_frame
                frame_seq = received.frame_seq
                folding, events = frame_projection.projected(folding, frame_seq=frame_seq, payload=received.payload)
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
                    await self._store.apply_frame(session_id, turn_id, frame_seq, events)
            result = completed.frame.payload
            failed = completed.event.outcome is TurnOutcome.FAILED and not abort_event.is_set()
            # `result.result` is deliberately not projected — it repeats the turn's last message on
            # every result frame, so minting prose from it would double every answer. It is handed
            # over as the fallback for the one case that is not a repeat: a turn whose text arrived
            # nowhere else. Which of the three cases this is, is `close_answer`'s to decide.
            #
            # **Before the turn is closed, not after.** Closing it makes it unadoptable, so a
            # replica dying between the two would strand the answer with nothing left to re-derive
            # it. **And before the failure is raised**, for the same reason at a shorter range: a
            # turn that produced text and then failed still produced the text, and the message it
            # is on is closed nowhere else — the ending frame's own events are not applied
            # (<../debug/message_drops.md> E4).
            said = await self._store.close_answer(
                session_id,
                turn_id,
                # A failing result's `result` is the failure rather than an answer, so nothing is
                # minted from it; what the turn already said stands on its own items.
                final_text="" if failed else str(result.get("result") or "").strip(),
                frame_seq=completed.frame.frame_seq,
            )
            if failed:
                # Quoted from the frame rather than the event: *why* a turn failed is
                # provider-specific by nature, and the neutral vocabulary carries an outcome
                # rather than a message on purpose.
                raise RuntimeError(
                    f"the agent's turn failed: {result.get('subtype')}: {result.get('stop_reason') or 'unknown error'}"
                )
            if not said and frontend is not None:
                # Every turn speaks, and there is deliberately no silence token: a turn that only
                # ran tools is legitimate, but it must not look like the console lost the answer.
                await frontend.report_silent_turn()
            await self._store.end_turn(
                turn_id,
                TurnOutcome.ABORTED if abort_event.is_set() else completed.event.outcome,
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
            await self._store.fail(session_id, str(error))
            raise
        finally:
            # The event outlives the turn (it is the session's), so only this turn's waiter goes.
            aborted.cancel()
            # Every terminal path, failure included: a line still saying "running Bash" after the
            # turn died is the stuck-indicator bug in another form.
            await status.finish()

    async def aclose(self) -> None:
        # Called from the lifespan on the way down. Handing every held lease back in one statement
        # is the guarantee the per-connection releases cannot be: a cancelled `handle_runner` may
        # not finish its own commit. Reachable only because `uvicorn.run` bounds
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
    and cannot tell which half a frame came from, so a turn whose ending is among the recorded
    frames closes without the socket being consulted (<README.md> § The cursor).
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
    actor: OperatorActorDep,
    store: SessionStoreDep,
    before_activity: Annotated[datetime | None, Query()] = None,
    before_conversation: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ConversationPage:
    """One page of this Operator's conversations, newest activity first.

    The two cursor parameters are the halves of `next_cursor` and travel together: either both or
    neither, because half a keyset is not a position.
    """
    if (before_activity is None) != (before_conversation is None):
        raise HTTPException(status_code=422, detail="before_activity and before_conversation go together")
    cursor = (
        None
        if before_activity is None or before_conversation is None
        else ConversationCursor(last_activity_at=before_activity, conversation_id=before_conversation)
    )
    return await store.list_operator_conversations(actor.operator_id, cursor=cursor, limit=limit)


@router.post("/api/conversations", status_code=201)
async def create_conversation(actor: OperatorActorDep, service: SessionServiceDep) -> ConversationView:
    """Open a new thread and the first session to run it.

    One call, because a conversation with no session is a thread nothing can be said to.
    """
    try:
        return await service.create_conversation(actor.operator_id)
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: UUID, actor: OperatorActorDep, service: SessionServiceDep
) -> ConversationView:
    try:
        return await service.conversation(actor.operator_id, conversation_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error


@router.get("/api/sessions/{session_id}/frames")
async def read_session_frames(
    session_id: UUID,
    actor: OperatorActorDep,
    store: SessionStoreDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_FRAME_PAGE)] = DEFAULT_FRAME_PAGE,
    kind: Annotated[list[str] | None, Query()] = None,
) -> SessionFramePage:
    """Claude Code's own protocol frames behind one session, newest page first.

    **One backend's wire, not the conversation.** These are the CLI's own frames in the CLI's own
    shapes — what `session_messages` is a lossy projection *of*. Nothing renders, announces or
    delivers from them.

    Omitting `before_seq` opens on the end of the log; the response's `next_before_seq` walks back
    from there.

    `kind` is repeatable and open, because the column is: the CLI may send a `type` this release
    has never heard of, and an inspector limited to a closed list would hide exactly the frame
    worth looking at. Omitting it means everything except `stream_event` — see
    `session_store._frames_of_kinds`.
    """
    try:
        return await store.read_operator_frames(
            actor.operator_id, session_id, before_seq=before_seq, limit=limit, kinds=kind
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error


@router.get("/api/sessions/{session_id}/provisioning")
async def read_session_provisioning(
    session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep
) -> SessionProvisioningView:
    """What Kubernetes says about the sandbox this session asked for, read live off the cluster.

    Addressed at a session rather than a conversation because a conversation runs several over its
    life, each with its own sandbox to account for. `GET /api/conversations/{conversation_id}`
    carries the same view for the current session and only while it is still provisioning; this
    answers for any session the Operator owns, in any state.
    """
    try:
        return await service.sandbox_provisioning(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error


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
) -> PromptAccepted:
    try:
        # Named rather than left to the default: the console's own surface is a channel like any
        # other, and a prompt typed here is one every attached room is owed a copy of.
        return PromptAccepted(item_id=await store.enqueue_prompt(actor.operator_id, session_id, body.text, SPA_ORIGIN))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    except PromptRefusedError as error:
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
