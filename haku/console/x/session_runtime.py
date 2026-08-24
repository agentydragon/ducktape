"""Operator chat sessions dispatched by immutable conversation runtime kind.

The turn loop, the runner's websocket bridge, the sandbox lifecycle and the SPA chat surface's own
routes. The rows underneath, and every transaction that moves them, are `session_store.py`.

The incidents behind this file's invariants are in <../debug/2026_08_16_runtime_archaeology.md>.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from haku.console.chat_models import (
    ENDED_SESSION_STATUSES,
    SPA_ORIGIN,
    BridgeFrameKind,
    FrameDirection,
    ItemType,
    PromptOrigin,
    RuntimeKind,
    SessionStatus,
    TurnOutcome,
)
from haku.console.operator_auth import OperatorActorDep
from haku.console.x.conversation_events import ConversationEvent
from haku.console.x.conversation_history import ConversationHistory
from haku.console.x.launch_identity import LaunchAgentRejectedError, LaunchAuthorizer
from haku.console.x.runtime import (
    Checkpoint,
    ConfiguredRuntime,
    OpenItemSeed,
    RuntimeAdapter,
    RuntimeClient,
    RuntimeLaunch,
    RuntimeMcpServer,
    RuntimeRegistry,
    RuntimeWakeWatcher,
    TurnCompletion,
    TurnProjectionSeed,
)
from haku.console.x.sandbox_claims import SandboxProvisioningView
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_store import (
    LEASE_RENEW_INTERVAL,
    BridgeAuthentication,
    PromptRecords,
    PromptRefusedError,
    ResumedTurn,
    SandboxDemand,
    SessionStore,
    TurnStart,
    WakeTurn,
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
from haku.console.x.system_prompt import HistoryMessage, SessionIntroduction
from haku.runtime.x.bridge.backend import MCP_CREDENTIAL_VARIABLE
from haku.runtime.x.bridge.client import ReceivedFrame, RecordedFrame
from haku.runtime.x.bridge.protocol import GOING_AWAY_CODE, NOT_ADMITTED_CODE, HarnessFrame, TextWebSocket

router = APIRouter(tags=["sessions"])
internal_router = APIRouter(tags=["session-runtime-internal"])
logger = logging.getLogger(__name__)

# How long one session's observed provisioning state is reused before the cluster is read again.
# Bounds what a polling browser costs the Kubernetes API server.
OBSERVATION_TTL = timedelta(seconds=2)

# The conversation tail a replacement session receives. Counted in finished prompts/answers, not
# transport events, and read from the console's record rather than any attached channel's copy.
RE_AWAKENING_MESSAGES = 20


# Aborts Postgres resolves by choosing a loser it expects to re-run: deadlock and serialization
# failure. The statements were not wrong; the interleaving was.
_RERUNNABLE_SQLSTATES = frozenset({"40001", "40P01"})


def _transient_database_error(error: BaseException) -> bool:
    """A database error that says nothing about the turn: the transaction never committed, and the
    same statements succeed on a healthy connection — a dropped connection (a CNPG failover or
    restart), or an abort Postgres asks the loser to re-run. Never an IntegrityError or a
    programming error, which fail identically on retry and so are the turn's own."""
    if not isinstance(error, DBAPIError):
        return False
    # `orig` is the dialect-adapted DBAPI error; the asyncpg adapter stamps the server's SQLSTATE
    # onto it as `sqlstate`, and `DBAPIError` offers no typed accessor for it.
    return (
        error.connection_invalidated
        or isinstance(error, (InterfaceError, OperationalError))
        or getattr(error.orig, "sqlstate", None) in _RERUNNABLE_SQLSTATES
    )


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


class ConversationCreateRequest(BaseModel):
    """Optional launch selectors; access profile is always derived server-side."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID | None = None
    runtime: RuntimeKind | None = None


class PromptAccepted(BaseModel):
    """The prompt is an item on the conversation, and a turn will take it.

    The id alone: the item's own rows reach the composer over the conversation's follow socket,
    which is where every other surface's prompts arrive too, so answering with a copy of it here
    would be the same prose by two routes.
    """

    item_id: UUID


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

    No native exclusions — control frames, because an interrupt that did not take is diagnosable
    from nothing else, and deltas, because a log with a hole in it cannot be folded over. Generic
    readers bound pages without classifying the payload; provider-aware interpretation stays in the
    selected integration.
    """

    def __init__(self, store: SessionStore, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, frame: HarnessFrame) -> int:
        return (await self._record(FrameDirection.TO_AGENT, frame.frame, kind=BridgeFrameKind.HARNESS_FRAME)).frame_seq

    async def received(self, frame: HarnessFrame) -> RecordedFrame:
        """Record the complete native harness frame and its bridge-owned position.

        All native frames, including deltas and opaque JSON-RPC notifications, are replayed and
        deduplicated by *runner_seq*. Their contents never participate in replay identity.

        *runner_seq* is kept beside the row's own `frame_seq` and read back as the session's resume
        cursor. Nothing orders by it.
        """
        return await self._record(
            FrameDirection.FROM_AGENT, frame.frame, runner_seq=frame.seq, kind=BridgeFrameKind.HARNESS_FRAME
        )

    async def _record(
        self,
        direction: FrameDirection,
        payload: dict[str, Any],
        *,
        runner_seq: int | None = None,
        kind: BridgeFrameKind = BridgeFrameKind.HARNESS_FRAME,
    ) -> RecordedFrame:
        return await self._store.record_frame(self._session_id, direction, kind, payload, runner_seq=runner_seq)


def _inherited(turn: TurnStart | ResumedTurn | WakeTurn) -> TurnProjectionSeed:
    """Where an adopting replica's fold picks the turn up.

    Empty only for a turn this process opened. What the store hands back is the open message, when
    there is one, so the prose lands on the same row either way; what this carries is how much of it
    has been said, without which the completed block arriving next is stored on top of the half
    already there. Materialised call ids are inherited independently of prose so completed
    compatibility blocks arriving after a roll stay duplicates rather than new calls.
    """
    if not isinstance(turn, ResumedTurn):
        return TurnProjectionSeed()
    if turn.streaming is None and turn.reasoning is None:
        return TurnProjectionSeed(seen_call_ids=turn.seen_call_ids, completed_call_ids=turn.completed_call_ids)
    return TurnProjectionSeed(
        open_message=(
            None
            if turn.streaming is None
            else OpenItemSeed(
                first_frame_seq=turn.streaming.first_frame_seq,
                last_frame_seq=turn.streaming.last_frame_seq,
                text=turn.streaming.text,
            )
        ),
        open_reasoning=(
            None
            if turn.reasoning is None
            else OpenItemSeed(
                first_frame_seq=turn.reasoning.first_frame_seq,
                last_frame_seq=turn.reasoning.last_frame_seq,
                text=turn.reasoning.text,
            )
        ),
        seen_call_ids=turn.seen_call_ids,
        completed_call_ids=turn.completed_call_ids,
    )


class SessionService:
    def __init__(
        self,
        runtimes: RuntimeRegistry,
        store: SessionStore,
        notifications: SessionNotifications,
        *,
        conversation_history: ConversationHistory | None = None,
        launch_authorizer: LaunchAuthorizer | None = None,
        default_agent_id: UUID | None = None,
    ):
        self._runtimes = runtimes
        self._store = store
        self._notifications = notifications
        self._conversation_history = conversation_history
        self._launch_authorizer = launch_authorizer
        self._default_agent_id = default_agent_id
        # Per session, the last view read off the cluster; `_observed` drops entries older than
        # `OBSERVATION_TTL` as it goes.
        self._observations: dict[UUID, SandboxProvisioningView] = {}

    async def _runtime(self, session_id: UUID) -> RuntimeAdapter:
        return self._runtimes[await self._store.runtime_kind_of(session_id)]

    async def _configured(self, session_id: UUID) -> ConfiguredRuntime:
        return self._runtimes.configured(await self._store.runtime_kind_of(session_id))

    async def request_abort(self, operator_id: UUID, session_id: UUID) -> bool:
        """Interrupt this session's turn, or answer False when it has none.

        Raises `KeyError` for a session this Operator does not own.
        """
        if not await self._store.session_exists(operator_id, session_id):
            raise KeyError(session_id)
        return await self._store.request_abort(session_id)

    async def create(
        self,
        operator_id: UUID,
        *,
        conversation_id: UUID | None = None,
        agent_id: UUID | None = None,
        runtime_kind: RuntimeKind | None = None,
    ) -> SessionView:
        if self._launch_authorizer is not None:
            selected_agent = agent_id or self._default_agent_id
            if conversation_id is None and selected_agent is None:
                raise RuntimeError("chat launch requires a selected Agent")
            # SessionStore owns the transaction.  It derives a replacement's pinned identity under
            # the conversation row lock and passes the same AsyncSession to the authorizer, so the
            # authorization decision and durable rows cannot be separated by a concurrent disable.
            view, token = await self._store.create_idle(
                operator_id,
                conversation_id=conversation_id,
                agent_id=selected_agent if conversation_id is None else None,
                runtime_kind=runtime_kind,
                launch_authorizer=self._launch_authorizer,
            )
        else:
            # Compatibility for direct unit-test callers and pre-identity local integrations.
            view, token = await self._store.create_idle(
                operator_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                runtime_kind=runtime_kind or RuntimeKind.CLAUDE_CODE,
            )
        assert not token, "an idle session must not expose a runner credential"
        return view

    async def ensure_session_for_demand(self, operator_id: UUID, conversation_id: UUID) -> SandboxDemand | None:
        """Create a demanded replacement under the pinned launch identity."""
        return await self._store.ensure_session_for_demand(
            operator_id, conversation_id, launch_authorizer=self._launch_authorizer
        )

    async def enqueue_prompt(
        self,
        operator_id: UUID,
        session_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept a prompt; the channel-neutral allocator reconciles its durable demand."""
        return await self._store.enqueue_prompt(operator_id, session_id, prompt_text, origin, records)

    async def enqueue_conversation_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept conversation-owned work without requiring a session to exist first."""
        return await self._store.enqueue_conversation_prompt(operator_id, conversation_id, prompt_text, origin, records)

    async def allocate(self, operator_id: UUID, session_id: UUID) -> bool:
        """Create the SandboxClaim for queued work exactly once across competing replicas."""
        allocation = await self._store.allocate(operator_id, session_id)
        if allocation is None:
            return False
        await self._create_claim(allocation.session_id, allocation.bridge_token)
        return True

    async def _create_claim(self, session_id: UUID, bridge_token: str) -> None:
        try:
            configured = await self._configured(session_id)
            resources = configured.resources
            await resources.claims.create(
                session_id=session_id,
                bridge_token=bridge_token,
                expires_at=datetime.now(UTC) + timedelta(seconds=resources.session_ttl_seconds),
            )
        except Exception as error:
            await self._store.fail(session_id, f"sandbox provisioning failed: {error}")
            # If claim creation reached Kubernetes before its response failed, remove the partial
            # resource now. A failed delete leaves `claim_cleaned_at` NULL, which is the durable
            # retry marker.
            await self._cleanup_terminal_claim(session_id)
            raise

    async def create_conversation(
        self, operator_id: UUID, *, agent_id: UUID | None = None, runtime_kind: RuntimeKind | None = None
    ) -> ConversationView:
        """Open a thread and the session that runs it, and read the thread back."""
        view = await self.create(operator_id, agent_id=agent_id, runtime_kind=runtime_kind)
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
        identity = await self._store.operator_session_identity(operator_id, session_id)
        return SessionProvisioningView(
            session_id=session_id,
            runtime_kind=identity.runtime_kind,
            status=identity.status,
            sandbox=None if identity.status == SessionStatus.IDLE else await self._observed(session_id),
        )

    async def provisioning_of(self, session_id: UUID, status: SessionStatus) -> SandboxProvisioningView | None:
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

    async def _observed(self, session_id: UUID) -> SandboxProvisioningView:
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
        configured = await self._configured(session_id)
        try:
            view = await configured.resources.claims.inspect(session_id=session_id)
        except Exception as error:
            view = configured.resources.claims.observation_error(session_id=session_id, error=str(error))
        self._observations[session_id] = view
        return view

    async def dispose(self, operator_id: UUID, session_id: UUID) -> None:
        await self._store.request_close(operator_id, session_id)
        await (await self._configured(session_id)).resources.claims.delete(session_id=session_id)
        await self._store.complete_claim_cleanup(session_id)

    async def reconcile_terminal_claims(self) -> None:
        """Finish idempotent claim cleanup left behind by an interrupted Console process."""

        session_ids = await self._store.claim_cleanup_candidates()
        for session_id in session_ids:
            await self._cleanup_terminal_claim(session_id)

    async def _cleanup_terminal_claim(self, session_id: UUID) -> bool:
        try:
            await (await self._configured(session_id)).resources.claims.delete(session_id=session_id)
        except Exception as error:
            # Leave `claim_cleaned_at` NULL so another replica or a later restart retries.
            # Kubernetes deletion is idempotent, so a redundant retry costs a 404.
            logger.warning("runtime claim cleanup failed for session %s: %s", session_id, error)
            return False
        await self._store.complete_claim_cleanup(session_id)
        return True

    async def _appended_prompt(self, session_id: UUID) -> str | None:
        """Who this session is, when its conversation has an attached chat surface.

        The conversation decides whether chat context applies; no channel object is handed to the
        session. The selected adapter decides how this addition is expressed without replacing the
        harness's own tool-driving preset.
        """
        resources = (await self._configured(session_id)).resources
        if self._conversation_history is None or not await self._store.attached(session_id):
            return None
        try:
            conversation_id = await self._store.conversation_of(session_id)
            recorded = await self._conversation_history.recent(
                conversation_id, before_session=session_id, limit=RE_AWAKENING_MESSAGES
            )
        except Exception:
            logger.exception("Could not read conversation history; starting session %s without it", session_id)
            recorded = ()
        return resources.system_prompt.render(
            SessionIntroduction(
                session_id=session_id,
                workspace=resources.cwd,
                recent_messages=tuple(
                    HistoryMessage(
                        sender="operator" if message.item_type is ItemType.PROMPT else "assistant",
                        body=message.body,
                        sent_at=message.sent_at,
                    )
                    for message in recorded
                ),
            )
        )

    def _progress_reporter(self, session_id: UUID) -> Callable[[str], Awaitable[None]]:
        """Record every sandbox progress report; subscribers decide how attached channels show it."""

        async def report(detail: str) -> None:
            logger.info("runtime sandbox %s: %s", session_id, detail)
            await self._store.narrate(session_id, detail)

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
        # the runtime client's reader routes it, and nothing is being read on this connection yet.
        configured = await self._configured(session_id)
        runtime = configured.adapter
        resources = configured.resources
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
            appended = await self._appended_prompt(session_id)
        except Exception as error:
            logger.exception("runtime system prompt failed to render for session %s", session_id)
            await self._store.fail(session_id, f"system prompt failed to render: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="system prompt failed to render")
            return
        # Launch assembly is deploy/config interpretation, so keep it on the admission side of
        # `accept()` too. A malformed endpoint must fail and release the claim rather than accept
        # the runner, escape the lifecycle handler below, and leave the session leased until its
        # sweeper deadline.
        try:
            launch = RuntimeLaunch(
                cwd=resources.cwd,
                environment=resources.environment,
                mcp_servers={
                    name: RuntimeMcpServer(url=url, bearer_environment_variable=MCP_CREDENTIAL_VARIABLE)
                    for name, url in resources.mcp_server_urls.items()
                },
                appended_system_prompt=appended,
                resume_from=await self._store.highest_runner_seq(session_id),
            )
            client = runtime.client(
                StarletteTextWebSocket(websocket),
                # The cursor is read here, per connection, off the session's own rows — so a replica
                # adopting a session mid-turn asks for what it is missing rather than being handed the
                # runner's whole replay window (<README.md> § `session_store.py` and `session_runtime.py`).
                launch,
                self._progress_reporter(session_id),
                RolloutRecorder(self._store, session_id),
            )
        except Exception as error:
            logger.exception("runtime launch preparation failed for session %s", session_id)
            await self._store.fail(session_id, f"runtime launch preparation failed: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="runtime launch preparation failed")
            return
        await websocket.accept()
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
                    # **One read of the stream in flight, ever, owned here.** An async generator
                    # refuses to be advanced twice at once and cancelling a read closes it, so the
                    # pending read survives idle waits and is handed into the turn that consumes it
                    # rather than being restarted. Declared before the try so the finally can always
                    # ask about it.
                    pending_frame: asyncio.Task[ReceivedFrame] | None = None
                    try:
                        await client.connect()
                        # One stream for the session, not one per turn: a folded prompt is answered
                        # with no second `result`, and an adopted turn was issued by a process that
                        # is gone. A turn is a bracket over this stream, not a request/response
                        # pair.
                        frames = _replaying(() if resumed is None else resumed.replay, client.frames().__aiter__())
                        watcher: RuntimeWakeWatcher | None = None
                        while True:
                            status = await self._store.status(session_id)
                            if status is None or status in ENDED_SESSION_STATUSES:
                                break
                            # The inherited turn before any new prompt, and once: its remaining
                            # frames are already on their way, so opening a second turn to take them
                            # would deliver one exchange's answer into another's bracket.
                            turn: TurnStart | ResumedTurn | WakeTurn | None = resumed
                            resumed = None
                            if turn is None:
                                turn = await self._store.next_prompt(session_id)
                            if turn is None:
                                # **An exchange has two legitimate initiators, so idle is one wait
                                # on both**: the operator's prompt queue, and the stream itself —
                                # the harness waking to observe work it left running. Whichever
                                # speaks first decides what the next turn is. A runtime with no
                                # wake watcher keeps the old contract: only the queue can start an
                                # exchange, and the stream is read only inside one.
                                if watcher is None:
                                    watcher = runtime.wake_watcher()
                                if watcher is None:
                                    # Wait for a LISTEN/NOTIFY instead of polling.
                                    await self._notifications.wait(
                                        SessionEventKind.PROMPT, session_id, timeout_seconds=30.0
                                    )
                                    continue
                                if pending_frame is None:
                                    pending_frame = asyncio.ensure_future(anext(frames))
                                prompted = asyncio.ensure_future(
                                    self._notifications.wait(SessionEventKind.PROMPT, session_id, timeout_seconds=30.0)
                                )
                                await asyncio.wait([pending_frame, prompted], return_when=asyncio.FIRST_COMPLETED)
                                prompted.cancel()
                                if not pending_frame.done():
                                    continue
                                received = pending_frame.result()
                                pending_frame = None
                                if (wake := watcher.observe(received.envelope)) is None:
                                    # Idle chatter — task bookkeeping, lifecycle markers. Consumed
                                    # and dropped: it projects to nothing, so an adoption replaying
                                    # over it loses nothing.
                                    continue
                                opened = await self._store.open_wake_turn(
                                    session_id, wake.description, first_frame_seq=received.frame_seq
                                )
                                if opened is None:
                                    continue
                                # The frame that began the exchange goes back on the front of the
                                # stream, so the turn loop reads the exchange from its first frame.
                                frames = _replaying((received,), frames)
                                turn = opened
                                watcher = None
                            # Cleared before the turn, not after: an abort notified just as the
                            # previous one ended would otherwise sit set through the idle wait and
                            # kill this turn on arrival.
                            abort_event.clear()
                            handed_frame, pending_frame = pending_frame, None
                            try:
                                await self._run_turn(
                                    client, frames, session_id, turn, abort_event=abort_event, pending=handed_frame
                                )
                            except Exception as error:
                                if _transient_database_error(error):
                                    # Says nothing about the session, so it gets the lost-runner
                                    # treatment rather than a terminal row: hand the session back,
                                    # and whichever replica the runner redials adopts the
                                    # still-open turn and replays it from the cursor.
                                    logger.exception(
                                        "transient database error mid-turn for session %s; leaving it for adoption",
                                        session_id,
                                    )
                                    keep_sandbox = True
                                    await self._store.release_lease(session_id)
                                    break
                                logger.exception("turn failed for session %s", session_id)
                                await self._store.fail(session_id, str(error))
                                break
                    finally:
                        abort_watch.cancel()
                        renewal.cancel()
                        connection.cancel()
                        # Closes the stream, which is fine: this connection is over either way.
                        if pending_frame is not None:
                            pending_frame.cancel()
            except* WebSocketDisconnect:
                # The runner went away, which is not the session being over: it keeps the CLI alive
                # across a lost socket and redials. Hand the session back and let the lease decide —
                # a runner that never returns leaves the row to the sweep.
                logger.info("session %s lost its runner; leaving it for adoption", session_id)
                keep_sandbox = True
                await self._store.release_lease(session_id)
            except* Exception as errors:
                # `fail` records the message; the traceback is what says which call produced it.
                logger.exception("%s runtime failed for session %s", runtime.display_name, session_id)
                await self._store.fail(session_id, f"{runtime.display_name} runtime failed: {_first_message(errors)}")
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

    async def _finalize(
        self, session_id: UUID, websocket: WebSocket, client: RuntimeClient, keep_sandbox: bool
    ) -> None:
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
        resources = (await self._configured(session_id)).resources
        while True:
            await self._store.renew_lease(session_id)
            await resources.claims.renew(
                session_id=session_id, expires_at=datetime.now(UTC) + timedelta(seconds=resources.session_ttl_seconds)
            )
            await asyncio.sleep(LEASE_RENEW_INTERVAL.total_seconds())

    async def _watch_connection(self, client: RuntimeClient) -> None:
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
        client: RuntimeClient,
        frames: AsyncIterator[ReceivedFrame],
        session_id: UUID,
        turn: TurnStart | ResumedTurn | WakeTurn,
        *,
        abort_event: asyncio.Event,
        pending: asyncio.Task[ReceivedFrame] | None = None,
    ) -> None:
        """Ask *turn*'s question if it has not been asked, then consume the stream until the turn
        completes.

        **Project, then act.** Every frame goes through the selected runtime adapter and this loop
        acts on the neutral events that come back, so what it knows about is prose, messages, tool calls
        and a completed turn rather than any harness's native frame vocabulary
        (<README.md> § The neutral projection).

        *frames* belongs to the session, not to this call — see `handle_runner`. This call is the
        turn's span and the only thing that closes it, so a turn left open means no code got to
        close it, which is what a replica losing its pod mid-exchange looks like from outside and
        what `ResumedTurn` picks back up.

        **No provider protocol state is interpreted here.** The selected handler privately carries
        the native fold for this turn; the loop sees only neutral effects and checkpoint decisions.
        Each non-terminal frame's effects are written with the projection cursor in one transaction
        (`SessionStore.apply_frame`), and `complete_frame` does the same for the terminal frame and
        turn close. A process dying anywhere therefore leaves the items saying what happened and
        the session saying which frame it got through — what makes adoption a replay from a durable
        position and its effects exactly-once (<README.md> § The cursor).
        """
        runtime = await self._runtime(session_id)
        turn_id = turn.turn_id
        if isinstance(turn, TurnStart):
            # A resumed turn's question was asked by a process that is gone, and a wake turn's was
            # never asked at all — the harness began the exchange itself. Either way only the
            # answer is still coming.
            await client.query(turn.prompt)
        # The provider owns every bit of protocol state. The generic loop gives it only durable
        # neutral facts inherited from the open turn and acts on the neutral effects it returns.
        handler = runtime.turn_handler(_inherited(turn))
        completion: TurnCompletion | None = None
        completion_frame_seq: int | None = None
        terminal_events: tuple[ConversationEvent, ...] = ()
        aborted = asyncio.ensure_future(abort_event.wait())
        # Set once the abort has been seen and the harness interrupted, from which point this loop
        # stops racing the abort event and drains what is left of the turn to its terminal frame.
        interrupted = False
        try:
            while completion is None:
                # Exactly one `anext` in flight, and the drain consumes the one it finds rather
                # than starting another: an async generator refuses to be advanced twice at once,
                # and an abort always arrives while this call is parked here. The caller's idle
                # wait may already hold that one read (`pending`); it is consumed first for the
                # same reason.
                next_frame = pending if pending is not None else asyncio.ensure_future(anext(frames))
                pending = None
                if not interrupted:
                    await asyncio.wait([next_frame, aborted], return_when=asyncio.FIRST_COMPLETED)
                    if interrupted := abort_event.is_set():
                        with contextlib.suppress(Exception):
                            await client.interrupt()
                # **The drain is this loop, not a second one beside it.** A harness may finish the
                # message it is mid-way through between the interrupt and its terminal frame; each
                # intervening frame is applied like any other
                # (<../debug/message_drops.md> E3). It therefore moves `said_anything` and
                # `queued_reply`, which is what keeps the tail below from owing the room the turn's
                # final text as well.
                #
                # The stream stays open for the next turn: it is the session's, so an interrupt
                # ends a turn rather than the conversation.
                received = await next_frame
                frame_seq = received.frame_seq
                effects = handler.apply(frame_seq=frame_seq, frame=received.envelope)
                # The frame that ends the turn goes no further: what is left of the exchange is
                # written below and `end_turn` is the transaction that closes it and carries the
                # cursor past this frame, so projecting it into `apply_frame` would advance the
                # cursor ahead of the turn's own last word.
                if effects.completion is not None:
                    completion = effects.completion
                    completion_frame_seq = frame_seq
                    terminal_events = effects.events
                elif effects.checkpoint is Checkpoint.HOLD:
                    # The integration has private state that is not durably representable yet. Keep
                    # the cursor before it so adoption rebuilds that state from the raw frames.
                    pass
                else:
                    await self._store.apply_frame(session_id, turn_id, frame_seq, effects.events)
            assert completion_frame_seq is not None
            failed = completion.failure is not None and not abort_event.is_set()
            # The terminal frame can carry ordinary durable effects as well as completion. They,
            # the answer close, the turn outcome and the cursor belong to one transaction: a split
            # would either lose terminal effects or let the cursor outrun the close on replica
            # death. The completion text remains only a fallback for a provider whose prose first
            # appears on that frame.
            await self._store.complete_frame(
                session_id,
                turn_id,
                completion_frame_seq,
                terminal_events,
                outcome=TurnOutcome.ABORTED if abort_event.is_set() else completion.outcome,
                final_text="" if failed else completion.final_text,
            )
            if failed:
                assert completion.failure is not None
                raise RuntimeError(completion.failure)
        except Exception as error:
            if _transient_database_error(error):
                # The transaction never committed, so the turn is still open and its cursor still
                # points before the unwritten frame. Closing it FAILED would record a permanent
                # outcome for an infrastructure blip; the caller hands the session back for
                # adoption instead, which replays the open turn from the cursor.
                raise
            # Bounded only where the failure was diagnosed from a terminal frame; otherwise this
            # turn ended on no frame of its own and `end_turn` bounds it by what it recorded.
            await self._store.end_turn(turn_id, TurnOutcome.FAILED, last_frame_seq=completion_frame_seq)
            await self._store.fail(session_id, str(error))
            raise
        finally:
            # The event outlives the turn (it is the session's), so only this turn's waiter goes.
            aborted.cancel()

    async def aclose(self) -> None:
        # Called from the lifespan on the way down. Handing every held lease back in one statement
        # is the guarantee the per-connection releases cannot be: a cancelled `handle_runner` may
        # not finish its own commit. Reachable only because `uvicorn.run` bounds
        # `timeout_graceful_shutdown` (see app.main).
        released = await self._store.release_held_leases()
        if released:
            logger.info("Released %d held session lease(s) on shutdown", released)
        await self._runtimes.aclose()


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
async def create_conversation(
    actor: OperatorActorDep, service: SessionServiceDep, body: ConversationCreateRequest | None = None
) -> ConversationView:
    """Open a new thread and the first session to run it.

    One call, because a conversation with no session is a thread nothing can be said to.
    """
    try:
        return await service.create_conversation(
            actor.operator_id,
            agent_id=None if body is None else body.agent_id,
            runtime_kind=None if body is None else body.runtime,
        )
    except LaunchAgentRejectedError:
        raise HTTPException(status_code=403, detail="chat launch is not authorized")
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error
    except Exception:
        logger.exception("conversation creation failed")
        raise HTTPException(status_code=503, detail="conversation service unavailable")


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
    kind: Annotated[list[BridgeFrameKind] | None, Query()] = None,
) -> SessionFramePage:
    """The native harness protocol frames behind one session, newest page first.

    **One backend's wire, not the conversation.** These are the CLI's own frames in the CLI's own
    shapes — what `session_messages` is a lossy projection *of*. Nothing renders, announces or
    delivers from them.

    Omitting `before_seq` opens on the end of the log; the response's `next_before_seq` walks back
    from there.

    Every native harness frame is returned without classification. The payload is forensic JSON;
    only the selected integration interprets its shape for conversation behavior.
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
    """What Kubernetes says about this session's sandbox, or no sandbox while it is idle.

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
    session_id: UUID, body: SessionPromptRequest, actor: OperatorActorDep, service: SessionServiceDep
) -> PromptAccepted:
    try:
        # Named rather than left to the default: the console's own surface is a channel like any
        # other, and a prompt typed here is one every attached room is owed a copy of.
        return PromptAccepted(
            item_id=await service.enqueue_prompt(actor.operator_id, session_id, body.text, SPA_ORIGIN)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    except PromptRefusedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post("/api/conversations/{conversation_id}/messages")
async def send_conversation_message(
    conversation_id: UUID, body: SessionPromptRequest, actor: OperatorActorDep, service: SessionServiceDep
) -> PromptAccepted:
    """Offer a prompt to a conversation even while no session is serving it.

    The session-addressed route remains during rollout for older bundles. New surfaces use this
    route, and the neutral conversation-runtime reconciler creates or reuses the session before the
    existing sandbox allocator provisions its container.
    """
    try:
        return PromptAccepted(
            item_id=await service.enqueue_conversation_prompt(actor.operator_id, conversation_id, body.text, SPA_ORIGIN)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="conversation not found") from error
    except PromptRefusedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


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
