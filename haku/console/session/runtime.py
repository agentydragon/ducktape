"""Operator sessions dispatched by their conversation's immutable harness kind.

The turn loop, the runner connection, the sandbox lifecycle and the SPA conversation surface's own
routes. The rows underneath, and every transaction that moves them, are `session_store.py`.

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from haku.console.conversation.history import ConversationHistory
from haku.console.conversation.item_vocabulary import ItemType
from haku.console.conversation.journal_consumer import JournalConsumer, JournalViolationError
from haku.console.conversation.prompt_origin import SPA_ORIGIN, PromptOrigin
from haku.console.database_retry import transient_database_error
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.operator_auth import OperatorActorDep
from haku.console.notifications.session_wakes import SessionEvent, SessionEventKind, SessionWakes
from haku.console.session.conversation_views import (
    DEFAULT_FRAME_PAGE,
    MAX_FRAME_PAGE,
    ConversationCursor,
    ConversationPage,
    ConversationView,
    SessionFramePage,
    SessionProvisioningView,
    SessionView,
)
from haku.console.session.launch_identity import LaunchAgentRejectedError, LaunchAuthorizer
from haku.console.session.sandbox_claims import ProvisioningStep, SandboxProvisioningView
from haku.console.session.session_frames import FrameDirection, SessionFrameKind
from haku.console.session.status import SessionStatus
from haku.console.session.store import (
    LEASE_RENEW_INTERVAL,
    PromptRecords,
    PromptRefusedError,
    RunnerConnectionAuthentication,
    SandboxDemand,
    Store,
)
from haku.console.session.system_prompt import HistoryMessage, HistorySender, SessionIntroduction
from haku.console.x.runtime import (
    ConfiguredHarness,
    HarnessKey,
    HarnessLaunchSpec,
    HarnessMcpServer,
    HarnessNotConfiguredError,
    HarnessRegistry,
    UnsupportedHarnessError,
)
from haku.runner.backend import LEGACY_SESSION_TOKEN_VARIABLE
from haku.runner.client import RecordedFrame
from haku.runner.neutral_operations import OperationBatch, RunnerHello
from haku.runner.protocol import (
    GOING_AWAY_CODE,
    NOT_ADMITTED_CODE,
    RUNNER_TO_CONSOLE,
    SUPPORTED_VERSIONS,
    ConsoleJournal,
    HarnessFrame,
    HarnessLaunch,
    Hello,
    Interrupt,
    PromptDispatch,
    RunnerJournal,
    SetupOutput,
    TextWebSocket,
)

router = APIRouter(tags=["sessions"])
internal_router = APIRouter(tags=["session-harness-internal"])
logger = logging.getLogger(__name__)

# How long one session's observed provisioning state is reused before the cluster is read again.
# Bounds what a polling browser costs the Kubernetes API server.
OBSERVATION_TTL = timedelta(seconds=2)

# The conversation tail a replacement session receives. Counted in finished prompts/answers, not
# transport events, and read from the console's record rather than any attached channel's copy.
RE_AWAKENING_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class ActiveSandboxRecord:
    """The operator-facing projection of one allocated session and its live claim graph."""

    session_id: UUID
    runtime_kind: HarnessKind
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    sandbox: SandboxProvisioningView


def _leaves(errors: BaseException) -> tuple[BaseException, ...]:
    """Every leaf of a possibly-nested `except*` group, flattened in order."""
    if isinstance(errors, BaseExceptionGroup):
        return tuple(leaf for exc in errors.exceptions for leaf in _leaves(exc))
    return (errors,)


def _first_message(errors: BaseExceptionGroup[Exception]) -> str:
    """The message of the first leaf in *errors*, for the operator-facing `error` column.

    `except*` hands back a group even for a single failure, and a group's own `str` is a count
    ("1 sub-exception").
    """
    leaves = _leaves(errors)
    return str(leaves[0]) if leaves else str(errors)


def _all_transient(errors: BaseExceptionGroup[Exception]) -> bool:
    """Whether every leaf is a transient database abort — a deadlock or serialization failure the
    runner will resume past on reconnect, or a dropped connection. Such a group leaves the session
    for adoption instead of failing it; a group with any other fault is the session's own."""
    leaves = _leaves(errors)
    return bool(leaves) and all(transient_database_error(leaf) for leaf in leaves)


class SessionPromptRequest(BaseModel):
    """What the SPA posts to send a prompt; the prompt itself becomes an item and a queued row."""

    text: str = Field(min_length=1, max_length=100_000)


class ConversationCreateRequest(BaseModel):
    """Explicit Web launch selectors; access profile is always derived server-side."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    harness_kind: HarnessKind


class PromptAccepted(BaseModel):
    """The prompt is in the durable inbox, and a runner will admit it into the transcript.

    The `prompt_id` alone (#4667): under the neutral-operation generation a prompt is a durable
    command before it is an item — the runner decides where it lands and `prompt.admitted`
    materialises it — so there is no item id to answer with yet, and the materialised item reaches
    the composer over the conversation's follow socket like every other surface's. The id is what a
    surface withdraws or correlates by.
    """

    prompt_id: UUID


class StarletteTextWebSocket(TextWebSocket):
    def __init__(self, websocket: WebSocket):
        self._websocket = websocket
        # The journal bridge has several concurrent senders on one socket — the ACK pump, the prompt
        # dispatcher, the interrupt relay — and Starlette's `send_text` is not concurrency-safe, so
        # a lone lock serialises writes. Reads are a single task and take no lock.
        self._send_lock = asyncio.Lock()

    async def send_text(self, data: str) -> None:
        async with self._send_lock:
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

    def __init__(self, store: Store, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, frame: HarnessFrame) -> int:
        return (await self._record(FrameDirection.TO_AGENT, frame.frame, kind=SessionFrameKind.HARNESS_FRAME)).frame_seq

    async def runner_frame(self, frame: HarnessFrame) -> RecordedFrame:
        """Record one journal-generation native frame the runner numbered (#4667).

        The runner numbers both directions now: a `to_agent` frame is native input the runner
        injected itself (the dispatched prompt, the interrupt) and echoed back under its own seq —
        `frame.injected` says which — and a `from_agent` frame is the CLI's own output. Both carry a
        runner seq, deduplicated by it on replay exactly as the v3 output stream was.
        """
        direction = FrameDirection.TO_AGENT if frame.injected else FrameDirection.FROM_AGENT
        return await self._record(direction, frame.frame, runner_seq=frame.seq, kind=SessionFrameKind.HARNESS_FRAME)

    async def received(self, frame: HarnessFrame) -> RecordedFrame:
        """Record the complete native harness frame and its runner-assigned position.

        All native frames, including deltas and opaque JSON-RPC notifications, are replayed and
        deduplicated by *runner_seq*. Their contents never participate in replay identity.

        *runner_seq* is kept beside the row's own `frame_seq` and read back as the session's resume
        cursor. Nothing orders by it.
        """
        return await self._record(
            FrameDirection.FROM_AGENT, frame.frame, runner_seq=frame.seq, kind=SessionFrameKind.HARNESS_FRAME
        )

    async def _record(
        self,
        direction: FrameDirection,
        payload: dict[str, Any],
        *,
        runner_seq: int | None = None,
        kind: SessionFrameKind = SessionFrameKind.HARNESS_FRAME,
    ) -> RecordedFrame:
        return await self._store.record_frame(self._session_id, direction, kind, payload, runner_seq=runner_seq)


class SessionService:
    def __init__(
        self,
        harnesses: HarnessRegistry,
        store: Store,
        notifications: SessionWakes,
        *,
        conversation_history: ConversationHistory | None = None,
        launch_authorizer: LaunchAuthorizer | None = None,
    ):
        self._harnesses = harnesses
        self._store = store
        self._notifications = notifications
        self._conversation_history = conversation_history
        self._launch_authorizer = launch_authorizer
        # The neutral-operation journal's commit/ACK/resume side (#4667). Its commits are the
        # store's transactions by another name, so it takes the same session factory.
        self._journal = JournalConsumer(store.sessionmaker)
        # Per session, the last view read off the cluster; `_observed` drops entries older than
        # `OBSERVATION_TTL` as it goes.
        self._observations: dict[UUID, SandboxProvisioningView] = {}

    def invalidate_sandbox_observations(self) -> None:
        """Drop cluster observations after a Kubernetes watch reports a lifecycle change."""
        self._observations.clear()

    async def _configured(self, session_id: UUID) -> ConfiguredHarness:
        identity = await self._store.session_identity(session_id)
        if identity.agent_id is None:
            raise HarnessNotConfiguredError("session has no pinned Agent/harness identity")
        return self._harnesses.configured(
            HarnessKey(identity.agent_id, identity.harness_kind), access_profile_id=identity.access_profile_id
        )

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
        harness_kind: HarnessKind | None = None,
    ) -> SessionView:
        if self._launch_authorizer is not None:
            if conversation_id is None and agent_id is None:
                raise RuntimeError("launch requires a selected Agent")
            if conversation_id is None and harness_kind is None:
                raise RuntimeError("launch requires a selected harness")
            # Store owns the transaction.  It derives a replacement's pinned identity under
            # the conversation row lock and passes the same AsyncSession to the authorizer, so the
            # authorization decision and durable rows cannot be separated by a concurrent disable.
            view, token = await self._store.create_idle(
                operator_id,
                conversation_id=conversation_id,
                agent_id=agent_id if conversation_id is None else None,
                harness_kind=harness_kind,
                launch_authorizer=self._launch_authorizer,
            )
        else:
            # Direct callers must provide the harness explicitly; there is no server default.
            access_profile_id = None
            if conversation_id is None:
                if agent_id is None:
                    raise RuntimeError("chat launch requires a selected Agent")
                if harness_kind is None:
                    raise RuntimeError("chat launch requires a selected harness")
                access_profile_id = self._harnesses.resources_for(HarnessKey(agent_id, harness_kind)).access_profile_id
                if access_profile_id is None:
                    raise HarnessNotConfiguredError("selected Agent has no configured access profile")
            view, token = await self._store.create_idle(
                operator_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                access_profile_id=access_profile_id,
                harness_kind=harness_kind,
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
        """Accept a prompt into the inbox for a session's conversation (#4667). Returns its id."""
        conversation_id = await self._store.conversation_of(session_id)
        return await self._store.submit_prompt(operator_id, conversation_id, prompt_text, origin, records)

    async def enqueue_conversation_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept conversation-owned work into the inbox without requiring a session first (#4667)."""
        return await self._store.submit_prompt(operator_id, conversation_id, prompt_text, origin, records)

    async def withdraw_prompt(self, operator_id: UUID, conversation_id: UUID, prompt_id: UUID) -> None:
        """Take a pending inbox prompt back (#4667). Raises `KeyError`/`PromptNotPendingError`."""
        await self._store.withdraw_prompt(operator_id, conversation_id, prompt_id)

    async def allocate(self, operator_id: UUID, session_id: UUID) -> bool:
        """Create the SandboxClaim for queued work exactly once across competing replicas."""
        allocation = await self._store.allocate(operator_id, session_id)
        if allocation is None:
            return False
        await self._create_claim(allocation.session_id, allocation.session_token)
        return True

    async def _create_claim(self, session_id: UUID, session_token: str) -> None:
        try:
            configured = await self._configured(session_id)
            resources = configured.resources
            await resources.claims.create(
                session_id=session_id,
                session_token=session_token,
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
        self, operator_id: UUID, *, agent_id: UUID | None = None, harness_kind: HarnessKind | None = None
    ) -> ConversationView:
        """Open a thread and the session that runs it, and read the thread back."""
        view = await self.create(operator_id, agent_id=agent_id, harness_kind=harness_kind)
        return await self.conversation(operator_id, await self._store.conversation_of(view.session_id))

    async def conversation(self, operator_id: UUID, conversation_id: UUID) -> ConversationView:
        view = await self._store.get_operator_conversation(operator_id, conversation_id)
        sandbox = await self.provisioning_of(view.session.session_id, view.session.status)
        return view.model_copy(update={"provisioning": sandbox})

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
            harness_kind=identity.harness_kind,
            status=identity.status,
            sandbox=None if identity.status == SessionStatus.IDLE else await self._observed(session_id),
        )

    async def list_active_sandboxes(
        self, operator_id: UUID, *, before_created_at: datetime | None, before_session_id: UUID | None, limit: int
    ) -> list[ActiveSandboxRecord]:
        """Project the operator's allocated sessions with their current claim observations."""
        records: list[ActiveSandboxRecord] = []
        cursor_created_at = before_created_at
        cursor_session_id = before_session_id
        excluded_session_id: UUID | None = None
        while len(records) < limit:
            requested = limit - len(records)
            sessions = await self._store.list_active_sessions(
                operator_id,
                before_created_at=cursor_created_at,
                before_session_id=cursor_session_id,
                limit=requested + (1 if excluded_session_id is not None else 0),
            )
            if excluded_session_id is not None:
                sessions = [session for session in sessions if session.session_id != excluded_session_id]
            if not sessions:
                break
            for session in sessions:
                sandbox = await self._observed(session.session_id)
                if sandbox.step is ProvisioningStep.CLAIM_ABSENT:
                    continue
                records.append(
                    ActiveSandboxRecord(
                        session_id=session.session_id,
                        runtime_kind=session.runtime_kind,
                        status=session.status,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                        sandbox=sandbox,
                    )
                )
                if len(records) >= limit:
                    break
            if len(sessions) < requested:
                break
            last = sessions[-1]
            cursor_created_at = last.created_at
            cursor_session_id = last.session_id
            excluded_session_id = last.session_id
        return records

    async def provisioning_of(self, session_id: UUID, status: SessionStatus) -> SandboxProvisioningView | None:
        """What Kubernetes says about a sandbox still coming up, for a session still waiting on one.

        Only while it is what the operator is waiting on, unlike `sandbox_provisioning`: this read
        goes out with every whole-conversation read and with every update a follower is sent, so
        asking for a session already past provisioning would put a cluster read on the transcript's
        hot path.

        **Not a fact about the conversation**, which is why it is read here and not in the store: it
        is an observation of another system, on that system's clock. No `conversation_event` row
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
        self.invalidate_sandbox_observations()
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
            logger.warning("harness claim cleanup failed for session %s: %s", session_id, error)
            return False
        await self._store.complete_claim_cleanup(session_id)
        return True

    async def _appended_prompt(self, session_id: UUID) -> str | None:
        """Who this session is, when its conversation has an attached channel.

        The conversation decides whether that context applies; no channel object is handed to the
        session. The selected adapter decides how this addition is expressed without replacing the
        harness's own tool-driving preset.
        """
        resources = (await self._configured(session_id)).resources
        if self._conversation_history is None or not await self._store.attached(session_id):
            return None
        # The conversation id is load-bearing prompt content, so its lookup failing fails the
        # render (and the admission path fails the session visibly); only the history reads
        # degrade to an empty tail.
        conversation_id = await self._store.conversation_of(session_id)
        try:
            recorded = await self._conversation_history.recent(
                conversation_id, before_session=session_id, limit=RE_AWAKENING_MESSAGES
            )
            earlier = await self._conversation_history.earlier_sessions(conversation_id, before_session=session_id)
        except Exception:
            logger.exception("Could not read conversation history; starting session %s without it", session_id)
            recorded = ()
            earlier = ()
        return resources.system_prompt.render(
            SessionIntroduction(
                session_id=session_id,
                conversation_id=conversation_id,
                workspace=resources.cwd,
                recent_messages=tuple(
                    HistoryMessage(
                        sender=HistorySender.OPERATOR
                        if message.item_type is ItemType.PROMPT
                        else HistorySender.ASSISTANT,
                        body=message.body,
                        sent_at=message.sent_at,
                    )
                    for message in recorded
                ),
                earlier_session_ids=earlier,
            )
        )

    async def handle_journal_runner(self, websocket: WebSocket, session_id: UUID, session_token: str) -> None:
        """Serve one runner at the neutral-operation generation (#4667 stage 4).

        The Console parses no native frames here: the runner interprets them and sends the
        acknowledged operation journal, which `JournalConsumer` commits and ACKs. Prompts are
        dispatched by durable id from the inbox and materialised on `prompt.admitted`; the operator's
        abort is relayed as an `Interrupt`; native frames are recorded to `session_frames` as the
        durable record beside the journal, both directions, keyed by the runner's frame seq.

        The serve loop is a journal pump, not a turn loop. Generation peering needs no gate here: a
        v3 peer fails the protocol-version intersection, and a v4 peer of another generation fails
        `JournalConsumer.resume`'s check of its journal hello.
        """
        try:
            configured = await self._configured(session_id)
        except (HarnessNotConfiguredError, UnsupportedHarnessError):
            await websocket.send_denial_response(
                Response(status_code=503, content=b"session harness is not configured on this replica")
            )
            return
        except KeyError:
            await websocket.close(code=NOT_ADMITTED_CODE, reason="invalid or consumed runner credential")
            return

        authentication = await self._store.authenticate_runner_connection(session_id, session_token)
        if authentication == RunnerConnectionAuthentication.HELD:
            await websocket.send_denial_response(
                Response(status_code=503, content=b"session is held by another replica")
            )
            return
        if authentication == RunnerConnectionAuthentication.TERMINAL:
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=NOT_ADMITTED_CODE, reason="runner session is already terminal")
            return
        if authentication == RunnerConnectionAuthentication.REJECTED:
            await websocket.close(code=NOT_ADMITTED_CODE, reason="invalid or consumed runner credential")
            return

        resources = configured.adapter, configured.resources
        harness, agent_resources = resources
        try:
            appended = await self._appended_prompt(session_id)
        except Exception as error:
            logger.exception("harness system prompt failed to render for session %s", session_id)
            await self._store.fail(session_id, f"system prompt failed to render: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="system prompt failed to render")
            return
        try:
            harness_launch = harness.build_launch(
                HarnessLaunchSpec(
                    cwd=agent_resources.cwd,
                    environment=agent_resources.environment,
                    mcp_servers={
                        # CLEANUP(added 2026-08-29): a sandbox claimed by a pre-rename console
                        # carries only the legacy variable, so the launch must reference the name
                        # every live pod has. Flip to SESSION_TOKEN_VARIABLE once no live sandbox
                        # predates the HAKU_SESSION_TOKEN rename — one release after it deploys.
                        name: HarnessMcpServer(url=url, bearer_environment_variable=LEGACY_SESSION_TOKEN_VARIABLE)
                        for name, url in agent_resources.mcp_server_urls.items()
                    },
                    appended_system_prompt=appended,
                    resume_from=await self._store.highest_runner_seq(session_id),
                )
            )
        except Exception as error:
            logger.exception("harness launch preparation failed for session %s", session_id)
            await self._store.fail(session_id, f"harness launch preparation failed: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="harness launch preparation failed")
            return

        await websocket.accept()
        text_ws = StarletteTextWebSocket(websocket)
        recorder = RolloutRecorder(self._store, session_id)
        keep_sandbox = False
        try:
            try:
                await self._journal_handshake(text_ws, session_id, harness_launch)
                async with asyncio.TaskGroup() as helpers:
                    abort_event = asyncio.Event()
                    # The helpers loop forever; the reader is what ends the group, by raising
                    # `WebSocketDisconnect` when the socket closes, which cancels the rest.
                    helpers.create_task(self._watch_aborts(session_id, abort_event))
                    helpers.create_task(self._renew_lease(session_id))
                    helpers.create_task(self._relay_interrupts(text_ws, abort_event))
                    helpers.create_task(self._dispatch_prompts(text_ws, session_id))
                    helpers.create_task(self._pump_journal(text_ws, session_id, recorder))
            except* WebSocketDisconnect:
                logger.info("session %s lost its runner; leaving it for adoption", session_id)
                keep_sandbox = True
                await self._store.release_lease(session_id)
            except* JournalViolationError as errors:
                # Terminal for the connection, not the session (`JournalConsumer` docstring): the
                # runner redials and resumes from the durable cursor, which fills a hole the drop
                # was about. A genuine contract breach loops with this log for an operator to read.
                logger.warning("session %s journal violation: %s", session_id, _first_message(errors))
                keep_sandbox = True
                await self._store.release_lease(session_id)
            except* Exception as errors:
                if _all_transient(errors):
                    # A transient database abort (a deadlock or serialization failure Postgres asked
                    # the loser to re-run, or a connection dropped under a failover) says nothing
                    # about the session: the batch's transaction rolled back and the runner resumes
                    # from the durable cursor on reconnect. `JournalConsumer.commit` retries these
                    # itself; one that still surfaces here leaves the session for adoption rather
                    # than failing the whole harness on an interleaving the next run will not hit.
                    logger.warning(
                        "session %s survived a transient database error: %s", session_id, _first_message(errors)
                    )
                    keep_sandbox = True
                    await self._store.release_lease(session_id)
                else:
                    logger.exception("%s journal harness failed for session %s", harness.display_name, session_id)
                    await self._store.fail(
                        session_id, f"{harness.display_name} harness failed: {_first_message(errors)}"
                    )
        except asyncio.CancelledError:
            keep_sandbox = True
            await self._store.release_lease(session_id)
            raise
        finally:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.shield(
                    asyncio.wait_for(self._finalize_journal(session_id, websocket, keep_sandbox), timeout=10)
                )

    async def _journal_handshake(self, websocket: TextWebSocket, session_id: UUID, launch: HarnessLaunch) -> None:
        """The two handshakes on every connection: the v3 version negotiation, then the journal's.

        The runner speaks first (its image is fixed): its `Hello` settles the protocol version, and
        the launch it is answered with carries the frame resume cursor. Then its `RunnerHello`
        carries the generation and journal versions, and `JournalConsumer.resume` answers where the
        session's durable batch cursor stands — from the row, so any replica agrees.
        """
        match RUNNER_TO_CONSOLE.validate_json(await websocket.receive_text()):
            case Hello(supported=supported):
                if not (common := set(supported) & set(SUPPORTED_VERSIONS)):
                    raise RuntimeError(
                        f"no protocol version in common: runner {supported}, console {SUPPORTED_VERSIONS}"
                    )
            case other:
                raise RuntimeError(f"runner sent {other.kind} before its hello")
        await websocket.send_text(launch.model_copy(update={"protocol_version": max(common)}).model_dump_json())
        match RUNNER_TO_CONSOLE.validate_json(await websocket.receive_text()):
            case RunnerJournal(message=RunnerHello() as hello):
                resume = await self._journal.resume(session_id, hello)
            case other:
                raise RuntimeError(f"runner sent {other.kind} before its journal hello")
        await websocket.send_text(ConsoleJournal(message=resume).model_dump_json())

    async def _pump_journal(self, websocket: TextWebSocket, session_id: UUID, recorder: RolloutRecorder) -> None:
        """Read the runner until its socket ends: record frames, commit batches, ACK each.

        The two streams are independent (#4667 amendment): a frame is recorded whether or not any
        operation names it, and a batch commits whether or not its provenance frames have arrived.

        `receive_text` raises `WebSocketDisconnect` when the runner's socket ends; it propagates to
        the handler's `except* WebSocketDisconnect`, which hands the session back for adoption.
        """
        while True:
            match RUNNER_TO_CONSOLE.validate_json(await websocket.receive_text()):
                case HarnessFrame() as frame:
                    await recorder.runner_frame(frame)
                case SetupOutput(data=data):
                    for line in data.decode(errors="replace").splitlines():
                        if stripped := line.strip():
                            await self._store.narrate(session_id, stripped)
                case RunnerJournal(message=OperationBatch() as batch):
                    ack = await self._journal.commit(session_id, batch)
                    await websocket.send_text(ConsoleJournal(message=ack).model_dump_json())
                case RunnerJournal(message=RunnerHello()):
                    raise RuntimeError("runner re-sent its journal hello mid-conversation")
                case Hello():
                    raise RuntimeError("runner re-sent its hello mid-conversation")

    async def _dispatch_prompts(self, websocket: TextWebSocket, session_id: UUID) -> None:
        """Send the runner every inbox prompt it is owed, now and whenever a new one arrives.

        Idempotent by `prompt_id`: the runner ignores an id it has taken, so re-sending a
        dispatched-but-unadmitted prompt after a reconnect is safe, and the per-connection set only
        spares the wire a repeat while the socket lives.
        """
        dispatched: set[UUID] = set()

        async def flush() -> None:
            for prompt in await self._store.pending_dispatch(session_id):
                if prompt.prompt_id not in dispatched:
                    dispatched.add(prompt.prompt_id)
                    await websocket.send_text(
                        PromptDispatch(prompt_id=prompt.prompt_id, text=prompt.text).model_dump_json()
                    )

        await flush()
        while True:
            await self._notifications.wait(session_id, timeout_seconds=30.0)
            await flush()

    async def _relay_interrupts(self, websocket: TextWebSocket, abort_event: asyncio.Event) -> None:
        """Turn each operator abort into one `Interrupt` on the wire, until cancelled."""
        while True:
            await abort_event.wait()
            abort_event.clear()
            await websocket.send_text(Interrupt().model_dump_json())

    async def _finalize_journal(self, session_id: UUID, websocket: WebSocket, keep_sandbox: bool) -> None:
        """Let go of one journal runner connection, and of the session unless it outlives us."""
        if keep_sandbox:
            with contextlib.suppress(Exception):
                await websocket.close(code=GOING_AWAY_CODE, reason="console replica going away")
            return
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

    async def _watch_aborts(self, session_id: UUID, abort_event: asyncio.Event) -> None:
        """Set *abort_event* every time this session is told to abort, until cancelled.

        The operator's abort lands on whichever replica the Service picks, rarely the one holding
        this session's websocket, so it arrives over NOTIFY rather than in process. An abort is an
        edge with no row to re-check, so this dispatches on the delivered kind rather than taking
        a plain wake — which is also what stops a listener reconnect (which wakes every waiter)
        from aborting an innocent turn. An abort emitted during a reconnect gap is lost, and the
        operator aborting again is the recovery.
        """

        def on_event(event: SessionEvent) -> None:
            if event.kind is SessionEventKind.ABORT:
                abort_event.set()

        with self._notifications.watch_session(session_id, on_event):
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        # Called from the lifespan on the way down. Handing every held lease back in one statement
        # is the guarantee the per-connection releases cannot be: a cancelled `handle_journal_runner`
        # may not finish its own commit. Reachable only because `uvicorn.run` bounds
        # `timeout_graceful_shutdown` (see app.main).
        released = await self._store.release_held_leases()
        if released:
            logger.info("Released %d held session lease(s) on shutdown", released)
        await self._harnesses.aclose()


def _service(request: Request) -> SessionService:
    service = cast(SessionService | None, request.app.state.session_service)
    if service is None:
        raise HTTPException(status_code=503, detail="the session harness is not configured")
    return service


def _store(request: Request) -> Store:
    store = cast(Store | None, request.app.state.session_store)
    if store is None:
        raise HTTPException(status_code=503, detail="the session harness is not configured")
    return store


def _session_wakes(request: Request) -> SessionWakes:
    session_wakes = cast(SessionWakes | None, request.app.state.session_wakes)
    if session_wakes is None:
        raise HTTPException(status_code=503, detail="the session harness is not configured")
    return session_wakes


SessionWakesDep = Annotated[SessionWakes, Depends(_session_wakes)]
SessionServiceDep = Annotated[SessionService, Depends(_service)]
StoreDep = Annotated[Store, Depends(_store)]


@router.get("/api/conversations")
async def list_conversations(
    actor: OperatorActorDep,
    store: StoreDep,
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
    body: ConversationCreateRequest, actor: OperatorActorDep, service: SessionServiceDep
) -> ConversationView:
    """Open a new thread and the first session to run it.

    One call, because a conversation with no session is a thread nothing can be said to. Agent and
    harness is an atomic required pair; there is no server-default launch endpoint.
    """
    try:
        return await service.create_conversation(
            actor.operator_id, agent_id=body.agent_id, harness_kind=body.harness_kind
        )
    except LaunchAgentRejectedError:
        raise HTTPException(status_code=403, detail="launch is not authorized")
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
    store: StoreDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_FRAME_PAGE)] = DEFAULT_FRAME_PAGE,
    kind: Annotated[list[SessionFrameKind] | None, Query()] = None,
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


@router.post("/api/conversations/{conversation_id}/messages", status_code=202)
async def send_conversation_message(
    conversation_id: UUID, body: SessionPromptRequest, actor: OperatorActorDep, service: SessionServiceDep
) -> PromptAccepted:
    """Offer a prompt to a conversation even while no session is serving it.

    The neutral conversation-harness reconciler creates or reuses the session before the existing
    sandbox allocator provisions its container.
    """
    try:
        return PromptAccepted(
            prompt_id=await service.enqueue_conversation_prompt(
                actor.operator_id, conversation_id, body.text, SPA_ORIGIN
            )
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
    scheme, _, session_token = authorization.partition(" ")
    if service is None or scheme.lower() != "bearer" or not session_token:
        await websocket.close(code=NOT_ADMITTED_CODE, reason="runner authentication required")
        return
    await service.handle_journal_runner(websocket, session_id, session_token)
