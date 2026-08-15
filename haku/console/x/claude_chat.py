"""Operator chat sessions backed by Claude Code in Agent Sandbox pods."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import decimal
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Protocol, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.cli_protocol.frame_identity import frame_uid
from haku.console.chat_models import (
    ENDED_SESSION_STATUSES,
    LIVE_SESSION_STATUSES,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSessionStatus,
    ChatSurface,
    FrameDirection,
    TurnOutcome,
)
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import (
    ClaudeChatFrame,
    ClaudeChatMessage,
    ClaudeChatPrompt,
    ClaudeChatSession,
    ClaudeChatTurn,
    ClaudeChatTurnPrompt,
)
from haku.console.operator_auth import OperatorActorDep
from haku.console.tools.conversations import Conversation, RolloutFrame, TurnRecord
from haku.console.x.chat_notifications import ChatEventKind, ChatNotifications, notify
from haku.runtime.x.claude_bridge.cli_client import ClaudeCli, cli_over_websocket
from haku.runtime.x.claude_bridge.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runtime.x.claude_bridge.protocol import GOING_AWAY_CODE, NOT_ADMITTED_CODE, TextWebSocket

router = APIRouter(tags=["claude-chat"])
internal_router = APIRouter(tags=["claude-chat-internal"])
logger = logging.getLogger(__name__)

# How long a live session stays believed-in after its holder last spoke, and how often that
# holder speaks. The gap absorbs a slow database round trip or a paused event loop without
# anyone reclaiming a session that is merely busy; the TTL bounds how long a room waits
# before being told the truth. A turn itself may run far longer than the TTL — the renewal
# is a separate task precisely so a long answer does not read as a dead replica.
LEASE_TTL = timedelta(seconds=90)
LEASE_RENEW_INTERVAL = timedelta(seconds=30)
# The creator's grant, covering the gap before a runner attaches and starts renewing. Longer
# than `LEASE_TTL` because it has to cover an image pull onto a cold node.
PROVISION_LEASE = timedelta(minutes=10)
# What a replica going down cleanly leaves behind: long enough for the runner to notice the
# socket close and redial onto whichever replica is up, short enough that a session nobody comes
# back for is reclaimed promptly. Shorter than `LEASE_TTL` because nothing is holding it — this
# is a window for an adopter to appear, not a heartbeat anyone is keeping.
ADOPTION_GRACE = timedelta(seconds=45)

# This process, as the lease records its holder. Kubernetes sets HOSTNAME to the pod name, which
# is what `kubectl logs` wants as an argument — so a session that died names the thing to go read.
REPLICA = os.environ.get("HOSTNAME", "unknown")

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


class BridgeAuthentication(StrEnum):
    ACCEPTED = "accepted"
    # The session is already over, so the runner should stop rather than retry.
    TERMINAL = "terminal"
    REJECTED = "rejected"


class ClaudeChatToolResultView(BaseModel):
    """What a tool answered, as the wire carried it.

    `content` is passed through rather than normalized: the CLI sends a bare string for most
    tools and a list of content blocks for those that return structured or mixed output, and
    collapsing the two here would be this layer deciding what a tool's output means.
    """

    model_config = ConfigDict(extra="forbid")

    content: Any
    is_error: bool = False


class ClaudeChatToolUseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    name: str
    input: dict[str, Any]
    # Absent while the call is still running, and on a turn that died before it answered — which
    # is a state worth seeing rather than one to hide. It comes from the rollout, because
    # `claude_chat_messages.tool_uses` never held it: the turn loop keeps the `tool_use` blocks
    # that asked and drops the `user` frames that answered.
    result: ClaudeChatToolResultView | None = None


class ClaudeChatMessageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    role: ChatMessageRole
    status: ChatMessageStatus
    content: str
    tool_uses: list[ClaudeChatToolUseView]
    error: str | None
    created_at: datetime
    updated_at: datetime


class ProvisioningStep(StrEnum):
    CLAIM_CREATED = "claim_created"
    WAITING_FOR_SANDBOX = "waiting_for_sandbox"
    WAITING_FOR_POD = "waiting_for_pod"
    WAITING_FOR_POD_READY = "waiting_for_pod_ready"
    WAITING_FOR_RUNNER = "waiting_for_runner"


class ClaudeSandboxProvisioningView(BaseModel):
    """Non-secret Kubernetes state explaining what sandbox provisioning is waiting on."""

    model_config = ConfigDict(extra="forbid")

    step: ProvisioningStep
    inspected_at: datetime
    claim_name: str
    claim_ready: bool | None = None
    claim_reason: str | None = None
    claim_message: str | None = None
    sandbox_name: str | None = None
    sandbox_ready: bool | None = None
    pod_name: str | None = None
    pod_phase: str | None = None
    pod_ready: bool | None = None
    runner_ready: bool | None = None
    runner_state: str | None = None
    observation_error: str | None = None


class ClaudeChatSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: ChatSessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    provisioning: ClaudeSandboxProvisioningView | None = None
    messages: list[ClaudeChatMessageView]


class ClaudeChatPromptRequest(BaseModel):
    """What the SPA posts to send a prompt. Named for the request, since the prompt itself is now
    a row (`database_schema.ClaudeChatPrompt`) rather than a field on the way in."""

    text: str = Field(min_length=1, max_length=100_000)


class SandboxClaims(Protocol):
    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None: ...

    async def delete(self, *, session_id: UUID) -> None: ...

    async def inspect(self, *, session_id: UUID) -> ClaudeSandboxProvisioningView: ...

    async def aclose(self) -> None: ...


class KubernetesSandboxClaims:
    """Create the narrow declarative SandboxClaim used by one chat session."""

    def __init__(self, config: ClaudeRuntimeConfig):
        self._config = config
        self._api_client: ApiClient | None = None
        self._custom_objects: CustomObjectsApi | None = None
        self._core_v1: CoreV1Api | None = None
        self._lock = asyncio.Lock()

    async def _clients(self) -> tuple[CustomObjectsApi, CoreV1Api]:
        if self._custom_objects is not None:
            assert self._core_v1 is not None
            return self._custom_objects, self._core_v1
        async with self._lock:
            if self._custom_objects is None:
                configuration = k8s_client.Configuration()
                try:
                    k8s_config.load_incluster_config(client_configuration=configuration)
                except ConfigException as error:
                    raise RuntimeError("Kubernetes in-cluster configuration is unavailable") from error
                self._api_client = ApiClient(configuration=configuration)
                self._custom_objects = CustomObjectsApi(self._api_client)
                self._core_v1 = CoreV1Api(self._api_client)
        assert self._custom_objects is not None
        assert self._core_v1 is not None
        return self._custom_objects, self._core_v1

    def _claim_name(self, session_id: UUID) -> str:
        return f"claude-{session_id.hex}"

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        body = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {
                "name": self._claim_name(session_id),
                "labels": {
                    "app.kubernetes.io/managed-by": "haku-console",
                    "haku.allegedly.works/runtime": "claude-chat",
                },
            },
            "spec": {
                "warmPoolRef": {"name": self._config.warm_pool},
                "lifecycle": {
                    "shutdownPolicy": "DeleteForeground",
                    "shutdownTime": expires_at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                },
                "env": [
                    {"name": "HAKU_CLAUDE_SESSION_ID", "value": str(session_id)},
                    {"name": "HAKU_AGENT_SDK_RUNNER_TOKEN", "value": bridge_token},
                ],
            },
        }
        client, _ = await self._clients()
        await client.create_namespaced_custom_object(
            "extensions.agents.x-k8s.io", "v1beta1", self._config.namespace, "sandboxclaims", body
        )

    async def delete(self, *, session_id: UUID) -> None:
        client, _ = await self._clients()
        try:
            await client.delete_namespaced_custom_object(
                "extensions.agents.x-k8s.io",
                "v1beta1",
                self._config.namespace,
                "sandboxclaims",
                self._claim_name(session_id),
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise

    async def inspect(self, *, session_id: UUID) -> ClaudeSandboxProvisioningView:
        claim_name = self._claim_name(session_id)
        custom_objects, core_v1 = await self._clients()
        try:
            claim = await custom_objects.get_namespaced_custom_object(
                "extensions.agents.x-k8s.io", "v1beta1", self._config.namespace, "sandboxclaims", claim_name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                return _provisioning_view(claim_name, step=ProvisioningStep.CLAIM_CREATED)
            raise

        claim_condition = _condition(claim, "Ready")
        claim_reason = _condition_text(claim_condition, "reason")
        claim_message = _condition_text(claim_condition, "message")
        sandbox_name = _nested_string(claim, "status", "sandbox", "name")
        if sandbox_name is None:
            return _provisioning_view(
                claim_name,
                step=ProvisioningStep.WAITING_FOR_SANDBOX,
                claim_ready=_condition_bool(claim_condition),
                claim_reason=claim_reason,
                claim_message=claim_message,
            )

        try:
            sandbox = await custom_objects.get_namespaced_custom_object(
                "agents.x-k8s.io", "v1beta1", self._config.namespace, "sandboxes", sandbox_name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                return _provisioning_view(
                    claim_name,
                    step=ProvisioningStep.WAITING_FOR_SANDBOX,
                    claim_ready=_condition_bool(claim_condition),
                    claim_reason=claim_reason,
                    claim_message=claim_message,
                    sandbox_name=sandbox_name,
                )
            raise

        sandbox_condition = _condition(sandbox, "Ready")
        annotations = sandbox.get("metadata", {}).get("annotations", {}) or {}
        pod_name = str(annotations.get("agents.x-k8s.io/pod-name") or sandbox_name)
        try:
            pod = await core_v1.read_namespaced_pod(pod_name, self._config.namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return _provisioning_view(
                    claim_name,
                    step=ProvisioningStep.WAITING_FOR_POD,
                    claim_ready=_condition_bool(claim_condition),
                    claim_reason=claim_reason,
                    claim_message=claim_message,
                    sandbox_name=sandbox_name,
                    sandbox_ready=_condition_bool(sandbox_condition),
                    pod_name=pod_name,
                )
            raise

        pod_phase = pod.status.phase if pod.status is not None else None
        pod_ready = _pod_ready(pod)
        runner_ready, runner_state = _container_status(pod, "runner")
        step = (
            ProvisioningStep.WAITING_FOR_RUNNER
            if pod_ready and runner_ready
            else ProvisioningStep.WAITING_FOR_POD_READY
        )
        return _provisioning_view(
            claim_name,
            step=step,
            claim_ready=_condition_bool(claim_condition),
            claim_reason=claim_reason,
            claim_message=claim_message,
            sandbox_name=sandbox_name,
            sandbox_ready=_condition_bool(sandbox_condition),
            pod_name=pod_name,
            pod_phase=pod_phase,
            pod_ready=pod_ready,
            runner_ready=runner_ready,
            runner_state=runner_state,
        )

    async def aclose(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None
            self._custom_objects = None
            self._core_v1 = None


@dataclass(frozen=True)
class SpaSession:
    """A session created by the browser chat view, which has no room."""

    # What the row records for this variant, carried on the variant rather than derived from it
    # by an `isinstance` chain at the one call site — where the enum and the room had to be
    # mapped separately, so a third surface would be two arms to remember rather than a field.
    surface_column: ClassVar[ChatSurface] = ChatSurface.SPA
    room_id: ClassVar[None] = None


@dataclass(frozen=True)
class MatrixSession:
    """A session created to serve one Matrix room, which it records for good.

    Carried as a variant rather than a `surface` enum beside an optional `room_id`, because
    the two combinations that pair would also admit — a Matrix session with no room, a room on
    an SPA session — are states no caller could act on. The table repeats the rule as a pair of
    check constraints, since the columns outlive this call signature.
    """

    surface_column: ClassVar[ChatSurface] = ChatSurface.MATRIX
    room_id: str


SessionSurface = SpaSession | MatrixSession


@dataclass(frozen=True, slots=True)
class TurnStart:
    """A prompt taken off the queue together with the turn opened to answer it."""

    turn_id: UUID
    message_id: UUID
    prompt: str


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Where a session got to, and why if it ended badly.

    The two travel together because every caller that acts on a dead session wants to say which
    one it was: the supervisor announced `ended (failed)` into the room for years while the
    sentence explaining it sat in `error`, reachable only by querying Postgres by hand.
    """

    status: ChatSessionStatus
    error: str | None


class ClaudeChatStore:
    """Async Postgres store for Claude chat sessions."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    @staticmethod
    def _fingerprint(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    async def create(self, operator_id: UUID, surface: SessionSurface) -> tuple[ClaudeChatSessionView, str]:
        now = datetime.now(UTC)
        session_id = uuid4()
        bridge_token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as db:
            db.add(
                ClaudeChatSession(
                    session_id=session_id,
                    operator_id=operator_id,
                    surface=surface.surface_column,
                    room_id=surface.room_id,
                    status=ChatSessionStatus.PROVISIONING,
                    bridge_token_fingerprint=self._fingerprint(bridge_token),
                    bridge_connected_at=None,
                    error=None,
                    # Granted by the creator, not by an owner: until a runner attaches there is
                    # no replica holding this session, and a sandbox that never comes up would
                    # otherwise sit in `provisioning` — a live status — with no lease to expire
                    # and so nothing to reclaim it. The window is the provisioning budget, and
                    # the owning replica takes over renewing it once the bridge connects.
                    lease_expires_at=now + PROVISION_LEASE,
                    created_at=now,
                    updated_at=now,
                )
            )
        view = await self.get(operator_id, session_id)
        return view, bridge_token

    async def get(self, operator_id: UUID, session_id: UUID) -> ClaudeChatSessionView:
        async with self._sessions() as db:
            record = await db.scalar(
                select(ClaudeChatSession).where(
                    ClaudeChatSession.session_id == session_id, ClaudeChatSession.operator_id == operator_id
                )
            )
            if record is None:
                raise KeyError(session_id)
            messages = list(
                (
                    await db.scalars(
                        select(ClaudeChatMessage)
                        .where(ClaudeChatMessage.session_id == session_id)
                        .order_by(ClaudeChatMessage.created_at, ClaudeChatMessage.message_id)
                    )
                ).all()
            )
            responding = await _open_turn(db, session_id) is not None
            return _session_view(record, messages, responding=responding, calls=await _rollout_calls(db, session_id))

    async def authenticate_bridge(self, session_id: UUID, token: str) -> BridgeAuthentication:
        """Admit a runner to its session — the first time, and every time after.

        **Taking the lease is the admission.** A reconnect used to be refused unconditionally
        (`PROVISIONING` and no `bridge_connected_at`), which made the sandbox disposable: the
        runner had no way back after any disconnect, so Kubernetes restarted it into a refusal
        until the sweep replaced the whole session. Now a live session admits a runner that can
        take its lease, and the lease is what stops both replicas adopting one CLI — whoever
        writes it under this row lock has it, and the other is told to go away for as long as
        the holder keeps renewing.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            record = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if record is None or not secrets.compare_digest(record.bridge_token_fingerprint, self._fingerprint(token)):
                return BridgeAuthentication.REJECTED
            if record.status in ENDED_SESSION_STATUSES:
                return BridgeAuthentication.TERMINAL
            if record.status == ChatSessionStatus.PROVISIONING and record.bridge_connected_at is None:
                record.bridge_connected_at = now
                record.status = ChatSessionStatus.READY
            elif record.lease_holder not in (None, REPLICA) and record.lease_expires_at > now:
                # Somebody else is still serving this session and saying so. Refusing is what
                # keeps one CLI answering to one console.
                return BridgeAuthentication.REJECTED
            record.lease_holder = REPLICA
            record.lease_expires_at = now + LEASE_TTL
            record.updated_at = now
            return BridgeAuthentication.ACCEPTED

    async def release_lease(self, session_id: UUID) -> None:
        """Hand a live session back for adoption, without declaring it dead.

        A replica going down cleanly knows the difference between "this session is over" and
        "I am no longer the one holding it", and only the second is true during a roll. Clearing
        the holder is what lets the returning runner be admitted immediately rather than waiting
        out the previous holder's lease.

        The short grant that replaces the deadline is what stops `expire_stale_leases` from
        failing the session in the seconds before the runner redials — and what makes it fail
        the session if none ever does.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if chat is not None and chat.status in LIVE_SESSION_STATUSES:
                chat.lease_holder = None
                chat.lease_expires_at = datetime.now(UTC) + ADOPTION_GRACE
                chat.updated_at = datetime.now(UTC)

    async def abandon_open_turn(self, session_id: UUID) -> UUID | None:
        """Close whatever turn the previous holder left open, and say which it was.

        An adopting console inherits the session, not the exchange: the frames answering that
        prompt went to a socket that no longer exists. Leaving it open would also wedge the
        session outright, since `uq_claude_chat_turns_open` permits exactly one and
        `next_prompt` opens another.

        **A turn that never asked its question gives the prompt back.** `next_prompt` claims the
        prompt and opens the turn in one transaction, and `_run_turn` writes it to the CLI
        afterwards; a replica dying between the two left the prompt claimed, the transcript row
        marked as handed over, and nothing ever asked — invisible, because the room's answer to
        a message it will never get is silence. That window is closed by re-queueing here.
        """
        async with self._sessions.begin() as db:
            turn = await db.scalar(
                select(ClaudeChatTurn)
                .where(ClaudeChatTurn.session_id == session_id, ClaudeChatTurn.ended_at.is_(None))
                .with_for_update()
            )
            if turn is None:
                return None
            turn_id = turn.turn_id
            if not await _prompt_left(db, session_id, turn.first_frame_seq):
                await _requeue(db, turn_id)
                await notify(db, ChatEventKind.PROMPT, session_id)
        await self.end_turn(turn_id, TurnOutcome.FAILED)
        return turn_id

    async def claim_cleanup_candidates(self) -> list[UUID]:
        """Return terminal sessions whose hashed rendezvous credential still marks cleanup pending."""
        async with self._sessions() as db:
            result = await db.scalars(
                select(ClaudeChatSession.session_id).where(
                    ClaudeChatSession.status.in_(ENDED_SESSION_STATUSES),
                    ClaudeChatSession.bridge_token_fingerprint != b"",
                )
            )
            return list(result.all())

    async def complete_claim_cleanup(self, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if chat is None:
                return
            chat.bridge_token_fingerprint = b""
            if chat.status == ChatSessionStatus.CLOSING:
                chat.status = ChatSessionStatus.CLOSED
            chat.updated_at = datetime.now(UTC)

    async def enqueue_prompt(self, operator_id: UUID, session_id: UUID, prompt_text: str) -> ClaudeChatMessageView:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.scalar(
                select(ClaudeChatSession)
                .where(ClaudeChatSession.session_id == session_id, ClaudeChatSession.operator_id == operator_id)
                .with_for_update()
            )
            if chat is None:
                raise KeyError(session_id)
            if chat.status != ChatSessionStatus.READY:
                raise RuntimeError(f"session is not ready (status={chat.status})")
            # Admission asks about the turn, not the session's status. It used to ask for
            # `READY`, which was also how "not mid-turn" was expressed — so the moment the
            # column stopped carrying `responding` this gate would have started accepting
            # prompts during a turn, which is the fold-into-turn feature arriving by accident
            # with no fold path wired (R2.2 holds a batch until the turn ends).
            if await _open_turn(db, session_id) is not None:
                raise RuntimeError("a turn is already in flight")
            if await _queued_prompt(db, session_id) is not None or await _legacy_pending(db, session_id) is not None:
                raise RuntimeError("a prompt is already queued")
            # Still minted here, and still `pending`: the transcript row is what the SPA gets back
            # from this call, and a replica on the previous image dequeues by finding that status.
            # Both stop being true in the contract release, where the row is written final and the
            # queue row alone says it is waiting.
            message = ClaudeChatMessage(
                message_id=uuid4(),
                session_id=session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.PENDING,
                content=prompt_text,
                tool_uses=[],
                error=None,
                created_at=now,
                updated_at=now,
            )
            db.add(message)
            db.add(
                ClaudeChatPrompt(prompt_id=uuid4(), session_id=session_id, message_id=message.message_id, queued_at=now)
            )
            # No status write: a queued prompt is not a turn in flight. Setting `responding`
            # here is what let `request_abort` accept an abort for a turn that did not exist.
            chat.updated_at = now
            await notify(db, ChatEventKind.PROMPT, session_id)
            await notify(db, ChatEventKind.UPDATE, session_id)
        return _message_view(message)

    async def next_prompt(self, session_id: UUID) -> TurnStart | None:
        """Take the queued prompt and open the turn that will answer it, or None if there is none.

        Dequeue and open are one transaction on purpose: they are the same event — the harness
        handing the agent a prompt — and splitting them would leave a window in which the prompt
        is claimed with no turn to name it, which is exactly what admission and abort now ask
        about.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return None
            now = datetime.now(UTC)
            # The queue first, then the transcript scan it replaces. Both, for the length of one
            # roll: a prompt an old replica accepted exists only as a `pending` message row, and
            # dropping the scan now would leave it accepted and never answered.
            if (queued := await _queued_prompt(db, session_id, lock=True)) is not None:
                queued.claimed_at = now
                message = await db.get(ClaudeChatMessage, queued.message_id)
                if message is None:
                    # The row the queue points at is gone, so there is no prompt to run and no
                    # text to run it with. Claiming it anyway is what stops the session retrying
                    # a prompt it can never read.
                    logger.error("Claude chat prompt %s has no message row", queued.prompt_id)
                    return None
            elif (message := await _legacy_pending(db, session_id, lock=True)) is None:
                return None
            message.status = ChatMessageStatus.COMPLETE
            message.updated_at = now
            chat.updated_at = now
            # The bracket's lower bound, taken before the prompt reaches the CLI so every frame
            # the exchange produces falls inside it.
            highest = await db.scalar(
                select(func.max(ClaudeChatFrame.frame_seq)).where(ClaudeChatFrame.session_id == session_id)
            )
            turn_id = uuid4()
            db.add(
                ClaudeChatTurn(
                    turn_id=turn_id, session_id=session_id, first_frame_seq=(highest or 0) + 1, started_at=now
                )
            )
            db.add(ClaudeChatTurnPrompt(turn_id=turn_id, message_id=message.message_id))
            await notify(db, ChatEventKind.UPDATE, session_id)
            return TurnStart(turn_id=turn_id, message_id=message.message_id, prompt=message.content)

    async def end_turn(self, turn_id: UUID, outcome: TurnOutcome, result: dict[str, Any] | None = None) -> None:
        """Close *turn_id*, taking the bracket's upper bound and what the `result` frame reported.

        Idempotent on an already-closed turn: a second close must not overwrite the first
        outcome, because the first one is the one that happened.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            turn = await db.get(ClaudeChatTurn, turn_id, with_for_update=True)
            if turn is None or turn.ended_at is not None:
                return
            turn.last_frame_seq = await db.scalar(
                select(func.max(ClaudeChatFrame.frame_seq)).where(ClaudeChatFrame.session_id == turn.session_id)
            )
            turn.ended_at = now
            turn.outcome = outcome
            if result is not None:
                # `total_cost_usd` is a float on the wire; through `Decimal(str(...))` rather than
                # `Decimal(float)`, which would carry the binary representation's noise into a
                # column that is exact on purpose.
                if isinstance(cost := result.get("total_cost_usd"), int | float):
                    turn.cost_usd = decimal.Decimal(str(cost))
                if isinstance(usage := result.get("usage"), dict):
                    turn.usage = usage
                if isinstance(duration := result.get("duration_ms"), int):
                    turn.duration_ms = duration
            chat = await db.get(ClaudeChatSession, turn.session_id)
            if chat is not None:
                # `responding` is derived from this turn being open, so closing it is what
                # retires the state — and what the SPA has to be told about. The column is only
                # written back when it still carries the old meaning, which a replica on the
                # previous image is what would have put there.
                if chat.status == ChatSessionStatus.RESPONDING:
                    chat.status = ChatSessionStatus.READY
                chat.updated_at = now
                await notify(db, ChatEventKind.UPDATE, turn.session_id)

    async def list_turns(self, session_id: str, *, limit: int) -> list[TurnRecord]:
        """A session's exchanges, newest first, for the `haku_conversations` read tools."""
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(ClaudeChatTurn)
                    .where(ClaudeChatTurn.session_id == UUID(session_id))
                    .order_by(ClaudeChatTurn.started_at.desc())
                    .limit(limit)
                )
            ).all()
        return [
            TurnRecord(
                turn_id=str(row.turn_id),
                first_frame_seq=row.first_frame_seq,
                last_frame_seq=row.last_frame_seq,
                started_at=row.started_at,
                ended_at=row.ended_at,
                outcome=row.outcome,
                cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
                duration_ms=row.duration_ms,
                usage=row.usage,
            )
            for row in rows
        ]

    async def record_frame(
        self, session_id: UUID, direction: FrameDirection, kind: str, payload: dict[str, Any]
    ) -> bool:
        """Append one frame to the session's rollout, unless this session already has it.

        Returns whether the frame was recorded. **False means a replay** — the same
        agent-assigned identity already in this session's log — and the caller's job is then to
        not act on it a second time, which is the whole point of deduplicating here rather than
        letting the row be written twice and reconciling later. A frame with no identity
        (`frame_identity.frame_uid` explains which those are) is always recorded, because "no
        identity" is not "the same as the last one".

        *kind* is passed rather than read out of the payload because the payload's discriminator
        is not the console's to assume: a CLI frame keeps it in `type`, the bridge envelope keeps
        it in `kind`, and deriving one from the other is what would make everything in this table
        have to look like a CLI frame whether it was one or not.

        Failures are not swallowed. Every other write in a turn reaches the same database, so
        one that cannot record has already lost the session — and a rollout with quiet holes is
        exactly the record that looks complete while being wrong, which is what this table
        exists to stop.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            # `ON CONFLICT DO NOTHING` against the partial unique index rather than a read
            # followed by a write: two replicas can be replaying the same buffer at once during
            # an adoption, and a check-then-insert would let both through.
            insert = (
                pg_insert(ClaudeChatFrame)
                .values(
                    session_id=session_id,
                    direction=direction,
                    kind=kind,
                    payload=payload,
                    partial=False,
                    frame_uid=frame_uid(kind, payload),
                    created_at=now,
                    updated_at=now,
                )
                # `index_where` as well as the columns, because the index is partial and Postgres
                # will not infer one without its predicate. A row whose `frame_uid` is NULL does
                # not satisfy that predicate, so it is simply inserted — which is the behaviour
                # "no identity is not the same as the last one" needs.
                .on_conflict_do_nothing(
                    index_elements=["session_id", "frame_uid"], index_where=text("frame_uid IS NOT NULL")
                )
            )
            recorded = cast("CursorResult[Any]", await db.execute(insert))
        return recorded.rowcount == 1

    async def list_conversations(self, *, limit: int) -> list[Conversation]:
        """Past sessions, newest first, for the `haku_conversations` read tools.

        Unscoped by R5.3a: every session, whichever room it served.
        """
        async with self._sessions() as db:
            rows = (
                await db.scalars(select(ClaudeChatSession).order_by(ClaudeChatSession.created_at.desc()).limit(limit))
            ).all()
        return [
            Conversation(
                session_id=str(row.session_id),
                surface=row.surface,
                room_id=row.room_id,
                status=row.status,
                created_at=row.created_at,
                error=row.error,
            )
            for row in rows
        ]

    async def read_frames(
        self, session_id: str, *, after_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]:
        """One page of a session's rollout, in wire order.

        Keyset paging on `frame_seq` rather than an offset: the log is append-only, so a cursor
        cannot skip or repeat a row the way an offset would once new frames land between pages.
        """
        query = select(ClaudeChatFrame).where(ClaudeChatFrame.session_id == UUID(session_id))
        if after_seq is not None:
            query = query.where(ClaudeChatFrame.frame_seq > after_seq)
        if kinds:
            query = query.where(ClaudeChatFrame.kind.in_(kinds))
        async with self._sessions() as db:
            rows = (await db.scalars(query.order_by(ClaudeChatFrame.frame_seq).limit(limit))).all()
        return [
            RolloutFrame(
                frame_seq=row.frame_seq,
                direction=row.direction,
                kind=row.kind,
                created_at=row.created_at,
                payload=row.payload,
                partial=row.partial,
            )
            for row in rows
        ]

    async def update_partial_frame(self, session_id: UUID, text: str) -> None:
        """Record the assistant message streaming right now, replacing any earlier state of it.

        Takes its `frame_seq` when the stream opens, so it sits where it belongs in the log
        even though it is rewritten afterwards.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            partial = await db.scalar(
                select(ClaudeChatFrame).where(ClaudeChatFrame.session_id == session_id, ClaudeChatFrame.partial)
            )
            if partial is None:
                db.add(
                    ClaudeChatFrame(
                        session_id=session_id,
                        direction=FrameDirection.FROM_AGENT,
                        kind="assistant",
                        payload=_assistant_frame(text),
                        partial=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            partial.payload = _assistant_frame(text)
            partial.updated_at = now

    async def clear_partial_frame(self, session_id: UUID) -> None:
        """Drop the reconstruction, now that the frame it stood in for has arrived."""
        async with self._sessions.begin() as db:
            await db.execute(
                delete(ClaudeChatFrame).where(ClaudeChatFrame.session_id == session_id, ClaudeChatFrame.partial)
            )

    async def begin_assistant(self, session_id: UUID) -> UUID:
        now = datetime.now(UTC)
        message_id = uuid4()
        async with self._sessions.begin() as db:
            db.add(
                ClaudeChatMessage(
                    message_id=message_id,
                    session_id=session_id,
                    role=ChatMessageRole.ASSISTANT,
                    status=ChatMessageStatus.STREAMING,
                    content="",
                    tool_uses=[],
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return message_id

    async def update_assistant(
        self,
        session_id: UUID,
        message_id: UUID,
        content: str,
        *,
        tool_uses: list[dict[str, Any]] | None = None,
        agent_message_id: str | None = None,
        complete: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            message = await db.get(ClaudeChatMessage, message_id)
            chat = await db.get(ClaudeChatSession, session_id)
            if message is None or chat is None:
                return
            message.content = content
            if tool_uses is not None:
                message.tool_uses = tool_uses
            if agent_message_id is not None:
                message.agent_message_id = agent_message_id
            message.status = ChatMessageStatus.COMPLETE if complete else ChatMessageStatus.STREAMING
            message.updated_at = now
            # No `chat.status = RESPONDING` here. This runs per stream delta, so it was a
            # session-row write per delta to hold a flag true that the open turn already states.
            chat.updated_at = now
            await notify(db, ChatEventKind.UPDATE, session_id)

    async def fail(self, session_id: UUID, error: str, message_id: UUID | None = None) -> None:
        # Logged as well as persisted. The column is the operator-facing record, but it is not
        # reachable from `kubectl logs`, and a Matrix session that dies leaves no other trace —
        # the room just stops answering. Diagnosing the asyncpg/psycopg listener mismatch that
        # killed every session meant querying this column out of Postgres by hand, purely
        # because the reason was written where logs are not.
        logger.error("Claude chat session %s failed: %s", session_id, error)
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is not None and chat.status not in {ChatSessionStatus.CLOSING, ChatSessionStatus.CLOSED}:
                chat.status = ChatSessionStatus.FAILED
                chat.error = error
                chat.updated_at = now
                await notify(db, ChatEventKind.UPDATE, session_id)
            if message_id is not None:
                message = await db.get(ClaudeChatMessage, message_id)
                if message is not None:
                    message.status = ChatMessageStatus.FAILED
                    message.error = error
                    message.updated_at = now

    async def request_close(self, operator_id: UUID, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.scalar(
                select(ClaudeChatSession)
                .where(ClaudeChatSession.session_id == session_id, ClaudeChatSession.operator_id == operator_id)
                .with_for_update()
            )
            if chat is None:
                raise KeyError(session_id)
            chat.status = ChatSessionStatus.CLOSING
            chat.updated_at = datetime.now(UTC)

    async def room_of(self, session_id: UUID) -> str | None:
        """The room this session was created to serve, or None if it serves none.

        The session's own record of it, not the current binding in `matrix_conversation`: that
        one moves to the next session the moment this one is replaced, so asking it "is this
        session mine?" answers about the room's present, not about the session.
        """
        async with self._sessions() as db:
            return await db.scalar(select(ClaudeChatSession.room_id).where(ClaudeChatSession.session_id == session_id))

    async def outcome(self, session_id: UUID) -> SessionOutcome | None:
        async with self._sessions() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            return None if chat is None else SessionOutcome(status=chat.status, error=chat.error)

    async def status(self, session_id: UUID) -> ChatSessionStatus | None:
        outcome = await self.outcome(session_id)
        return outcome.status if outcome is not None else None

    async def renew_lease(self, session_id: UUID) -> None:
        """Assert that this replica still holds *session_id* and is still working on it.

        Writes the holder as well as the deadline, because the renewal *is* the claim: the row
        goes from the creator's unheld provisioning grant to this pod's heartbeat the first time
        the replica running the turn says so, and nothing else has to sequence that.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is not None and chat.status in LIVE_SESSION_STATUSES:
                chat.lease_expires_at = datetime.now(UTC) + LEASE_TTL
                chat.lease_holder = REPLICA

    async def expire_stale_leases(self) -> int:
        """Fail every live session whose holder stopped renewing, and report how many.

        A live status is written by the replica holding the runner websocket and only ever
        corrected by that same replica. When it dies without running its finalizer — SIGKILL,
        OOM, node loss, or a cancellation that raced the event loop's shutdown — the row keeps
        claiming a turn is in flight, `supervise_once` treats it as healthy, and the room is
        never answered again. Nothing in the previous design could observe that, because every
        observer was the process that had gone away.

        Set-based and idempotent, in the shape `node_daemons._expire` already uses: any replica
        may run it, concurrent runners converge, and a session whose owner is merely slow gets
        its lease back on the next renewal well before the TTL.

        No null check: the column is required (0029), so "has no lease" is unrepresentable
        rather than a case to filter for. It used to be both representable and invisible here,
        which is how a session predating the column stayed wedged after the lease shipped.
        """
        async with self._sessions.begin() as db:
            expired = (
                await db.scalars(
                    select(ClaudeChatSession.session_id).where(
                        ClaudeChatSession.status.in_(LIVE_SESSION_STATUSES),
                        ClaudeChatSession.lease_expires_at <= datetime.now(UTC),
                    )
                )
            ).all()
            for session_id in expired:
                # Row-at-a-time rather than one UPDATE: `notify` is per session, and a room
                # that is not told its session died is exactly the silence being fixed here.
                chat = await db.get(ClaudeChatSession, session_id, with_for_update=True)
                if chat is None or chat.status not in LIVE_SESSION_STATUSES:
                    continue
                # Naming the holder is the whole point of recording it: this message and the
                # `error` below were previously identical for every such failure, so the room
                # said a session died and no query could say which process to go read.
                held_by = chat.lease_holder or "no replica (never attached)"
                logger.error("Claude chat session %s lease expired; holder was %s", session_id, held_by)
                chat.status = ChatSessionStatus.FAILED
                chat.error = f"console replica holding this session went away mid-turn ({held_by})"
                chat.updated_at = datetime.now(UTC)
                await notify(db, ChatEventKind.UPDATE, session_id)
            return len(expired)

    async def closed(self, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is not None and chat.status != ChatSessionStatus.FAILED:
                chat.status = ChatSessionStatus.CLOSED
                chat.updated_at = datetime.now(UTC)
                await notify(db, ChatEventKind.UPDATE, session_id)

    async def session_exists(self, operator_id: UUID, session_id: UUID) -> bool:
        async with self._sessions() as db:
            return (
                await db.scalar(
                    select(ClaudeChatSession.session_id).where(
                        ClaudeChatSession.session_id == session_id, ClaudeChatSession.operator_id == operator_id
                    )
                )
                is not None
            )

    async def request_abort(self, session_id: UUID) -> bool:
        """Ask whichever replica is running this session's turn to interrupt it.

        Returns False when no turn is in flight. This goes through NOTIFY rather than an
        in-process registry because the two ends land on different replicas: the abort event
        belongs to the pod holding the runner's bridge websocket, while the operator's HTTP
        request is balanced across all of them.
        """
        async with self._sessions.begin() as db:
            if await _open_turn(db, session_id) is None:
                return False
            await notify(db, ChatEventKind.ABORT, session_id)
            return True


class StarletteTextWebSocket(TextWebSocket):
    def __init__(self, websocket: WebSocket):
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        await self._websocket.send_text(data)

    async def receive_text(self) -> str:
        return await self._websocket.receive_text()

    async def close(self) -> None:
        await self._websocket.close()


# The agent's protocol frames only, so a `SetupOutput` carrying bootstrap narration is not
# mistaken for one. `stream_event` is the one exclusion: it is a partial of a frame that also
# arrives complete, thousands of times a turn (R5.5b) — being streamed is not the reason, and
# the completed `assistant` frame beside it is recorded like anything else.
_PARTIAL_FRAME_KIND = "stream_event"

# The frame a prompt crosses the wire as. Only meaningful with a direction beside it: the CLI
# sends `user` frames too, carrying tool results.
PROMPT_FRAME_KIND = "user"


def _assistant_frame(text: str) -> dict[str, Any]:
    """The frame shape the agent will send, for the one the console stands in for meanwhile.

    Same shape as the wire's, so a reader needs no second case; the row's `partial` column is
    what says it was reconstructed rather than observed.
    """
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


SETUP_OUTPUT_KIND = "setup_output"


def _setup_output_frame(text: str) -> dict[str, Any]:
    """One line the sandbox printed, as a rollout row.

    **Console-authored, like `partial`, and it says so with its discriminator.** The bridge's
    own frame is `SetupOutput(data: bytes)` — raw, unsplit, base64 on the wire — and what
    arrives here is one line the transport has already decoded (`errors="replace"`) and split
    for the room. So this is a rendering, not the wire, and putting it under `kind` rather than
    the CLI's `type` is what keeps it from reading as a protocol frame that never existed.

    It lives in the frame log rather than a table of its own because the question a reader asks
    is "what happened in this session, in order" — and for a session that died before the CLI
    produced anything, the answer is entirely here.
    """
    return {"kind": SETUP_OUTPUT_KIND, "text": text}


def _frame_kind(payload: dict[str, Any]) -> str:
    kind = payload.get("type")
    if not isinstance(kind, str):
        raise ValueError(f"protocol frame has no type: {payload=}")
    return kind


class RolloutRecorder:
    """One session's `FrameSink`: every protocol frame either way, into `claude_chat_frames`.

    This is the whole of what the console asks of the record, and both decisions in it are the
    console's rather than the protocol client's. **Deltas are skipped** because the store keeps a
    single rewritten `partial` row for the answer in flight instead — thousands of
    `content_block_delta` frames would bury the log for a reader and say nothing the completed
    `assistant` frame does not. Everything else is kept verbatim, control frames included, since
    an interrupt that did not take is only diagnosable from them.
    """

    def __init__(self, store: ClaudeChatStore, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, payload: dict[str, Any]) -> None:
        await self._record(FrameDirection.TO_AGENT, payload)

    async def received(self, payload: dict[str, Any]) -> bool:
        return await self._record(FrameDirection.FROM_AGENT, payload)

    async def _record(self, direction: FrameDirection, payload: dict[str, Any]) -> bool:
        """Record the frame, answering whether the caller should act on it.

        A delta is skipped and reported as fresh: the store keeps one rewritten `partial` row for
        the answer in flight instead, and the turn loop still has to append it. Only a frame the
        log already holds under the same agent-assigned identity is a replay.
        """
        if _frame_kind(payload) == _PARTIAL_FRAME_KIND:
            return True
        return await self._store.record_frame(self._session_id, direction, _frame_kind(payload), payload)


# How long a turn runs before the room is told anything about it (R6.2). Below this the
# answer itself is the status, and a status/answer pair for a five-second exchange is
# clutter.
STATUS_AFTER_SECONDS = 8.0

# How often a running turn re-asserts its typing notice. Comfortably inside the homeserver's
# expiry (`matrix_client.TYPING_TIMEOUT_MS`, 30s), because the point of that expiry is to retire
# the indicator when the console dies — not to blink it off mid-turn while it is still going.
TYPING_REFRESH_SECONDS = 10.0

# Floor on how often the room's status line is rewritten. Paced for a reader and for Synapse's
# per-room rate limit, not for how fast the agent changes what it is doing.
#
# Here rather than at the send, because a floor and a "what should it say" have to be one
# decision: a sink that silently declines to send inside its own floor loses the state the
# driver had already recorded as shown. This is the driver's to defer, and the eventual
# room-wide pacer takes it over along with every other sender.
STATUS_EDIT_INTERVAL_SECONDS = 5.0


def _coarse_status(frame: dict[str, Any]) -> str | None:
    """What the room should be told this frame means, or None if it means nothing to it.

    Coarse by rule, not by taste (R6.3): where a tool is named, the CLI's own identifier is
    passed through verbatim, and where the CLI wrote a human-readable description of a task
    it is used as-is. There is deliberately no per-tool copy and no mapping table, because
    both would need maintaining every time the tool surface grows.
    """
    match frame.get("type"):
        case "assistant":
            names = [block["name"] for block in _content_blocks(frame) if block.get("type") == "tool_use"]
            return f"running {', '.join(names)}" if names else "writing"
        case "system":
            match frame.get("subtype"):
                # `description` here is the CLI's own prose for the step in flight, e.g.
                # "Running Count regular files in the directory" — better than anything the
                # console could reconstruct from a tool name and its arguments.
                case "task_started" | "task_progress":
                    return str(frame.get("description") or "working")
    return None


async def _ignore_status(text: str) -> None:
    del text


async def _ignore_clear() -> None:
    pass


async def _ignore_typing(active: bool) -> None:
    del active


class _TurnStatus:
    """Drives what the room shows while one turn runs: the typing indicator and the status line.

    A polled driver rather than a write on every frame, because everything that decides whether
    to speak is about elapsed time — the typing notice's expiry, the status line's lazy-creation
    threshold, its edit floor — and a turn can go a long while between frames. Frames set the
    state; the loop decides when the room hears about it.

    The two differ in when they start. Typing goes on immediately, because "Haku is working on
    it" is the whole message and it is worth nothing after the fact; the status line waits for
    `STATUS_AFTER_SECONDS`, because a status/answer pair for a five-second exchange is clutter.
    """

    def __init__(
        self,
        show: Callable[[str], Awaitable[None]],
        clear: Callable[[], Awaitable[None]],
        typing: Callable[[bool], Awaitable[None]] = _ignore_typing,
    ):
        self._show = show
        self._clear = clear
        self._typing = typing
        self._state: str | None = None
        # What the room was last told, so an unchanged state is not re-sent every tick. The sync
        # service drops a repeat anyway, but a driver that says the same thing once a second is
        # relying on that rather than meaning it.
        self._shown: str | None = None
        self._started = time.monotonic()
        self._shown_at = 0.0
        self._typed_at = 0.0
        self._task: asyncio.Task[None] | None = None

    def note(self, frame: dict[str, Any]) -> None:
        if (state := _coarse_status(frame)) is not None:
            self._state = state

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            # Refreshed rather than set once: the homeserver expires a typing notice by itself,
            # which is what stops a dead console from leaving one stuck on — so a live turn has
            # to keep saying it. Well inside `TYPING_TIMEOUT_MS`, so a slow round trip does not
            # leave a gap the operator can see.
            if time.monotonic() - self._typed_at >= TYPING_REFRESH_SECONDS:
                self._typed_at = time.monotonic()
                await self._typing(True)
            # One owner for the pace. The floor used to be the sink's, which dropped anything
            # offered inside it while this loop had already recorded it as shown — so a state
            # that changed twice within the floor left the room reading the older of the two
            # until the *next* change, which on a turn that then settles into one long tool call
            # is the rest of the turn. Deferring here instead means the value is still waiting on
            # the next tick.
            if (
                self._state is not None
                and self._state != self._shown
                and time.monotonic() - self._started >= STATUS_AFTER_SECONDS
                and time.monotonic() - self._shown_at >= STATUS_EDIT_INTERVAL_SECONDS
            ):
                self._shown, self._shown_at = self._state, time.monotonic()
                await self._show(self._state)
            await asyncio.sleep(1.0)

    async def finish(self) -> None:
        """Stop driving and take both back, on every path out of the turn including failure."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._typing(False)
        await self._clear()


class RoomSurface(Protocol):
    """The front end for sessions that serve a room, for the parts a turn cannot do itself.

    The SPA needs none of this: its client reads the message rows over SSE, so a finished turn
    is already delivered by being written down. A room has to be spoken to, and told who it is
    talking to.

    **The service picks this by reading the session's `surface`, rather than offering every
    session to every listener.** It used to be three optional callbacks fired for all sessions,
    each implementation opening with the same four lines — load the current room binding,
    compare its `session_id`, return if it did not match. That is the session row's own fact
    (`surface`, `room_id`), so re-deriving it at three call sites was both a lookup per
    delivery and a way for a mis-derived answer to fail silently: a surface that wrongly
    decided a session was not its own simply said nothing.
    """

    async def system_prompt(self, session_id: UUID, room_id: str) -> str: ...

    async def deliver(self, room_id: str, text: str) -> None: ...

    async def report(self, room_id: str, detail: str) -> None: ...

    async def show_status(self, room_id: str, text: str) -> None: ...

    async def clear_status(self, room_id: str) -> None: ...

    async def set_typing(self, room_id: str, active: bool) -> None: ...


class ClaudeChatService:
    def __init__(
        self,
        config: ClaudeRuntimeConfig,
        store: ClaudeChatStore,
        claims: SandboxClaims,
        notifications: ChatNotifications,
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

    async def create(self, operator_id: UUID, surface: SessionSurface) -> ClaudeChatSessionView:
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

    async def get(self, operator_id: UUID, session_id: UUID) -> ClaudeChatSessionView:
        view = await self._store.get(operator_id, session_id)
        return await self._with_provisioning(view)

    async def _with_provisioning(self, view: ClaudeChatSessionView) -> ClaudeChatSessionView:
        if view.status != ChatSessionStatus.PROVISIONING:
            return view
        try:
            provisioning = await self._claims.inspect(session_id=view.session_id)
        except Exception as error:
            provisioning = _provisioning_view(
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

    def _turn_status(self, room_id: str | None) -> _TurnStatus:
        """A status driver for one turn, wired to the room if this session serves one.

        A session with no room still gets a driver rather than a `None` to branch on: the SPA
        reads the message rows, so there is simply nothing for its status to do, and the turn
        loop should not have to know which surface it is on.
        """
        surface, room = self._room_surface, room_id
        if surface is None or room is None:
            return _TurnStatus(_ignore_status, _ignore_clear)
        return _TurnStatus(
            lambda text: surface.show_status(room, text),
            lambda: surface.clear_status(room),
            lambda active: surface.set_typing(room, active),
        )

    def _progress_reporter(self, session_id: UUID, room_id: str | None) -> Callable[[str], Awaitable[None]]:
        """Record every sandbox progress report, log it, and show it to the room if there is one.

        Recorded first because the rollout is the only durable copy. This narration is where a
        bootstrap says why it failed and where the CLI's own stderr now arrives, and until this
        it lived in the pod's log and in the room — the first reaped with the sandbox, the
        second interleaved with everything else. A session that died before producing a single
        CLI frame therefore explained itself nowhere.
        """

        async def report(detail: str) -> None:
            logger.info("Claude sandbox %s: %s", session_id, detail)
            await self._store.record_frame(
                session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, _setup_output_frame(detail)
            )
            if self._room_surface is not None and room_id is not None:
                await self._room_surface.report(room_id, detail)

        return report

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == BridgeAuthentication.TERMINAL:
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=NOT_ADMITTED_CODE, reason="runner session is already terminal")
            return
        if authentication == BridgeAuthentication.REJECTED:
            await websocket.close(code=NOT_ADMITTED_CODE, reason="invalid or consumed runner credential")
            return
        # Whatever the previous holder was in the middle of is not ours to finish: its frames
        # went to a socket that is gone. Closing it is also what keeps the session usable, since
        # only one turn may be open at a time.
        if (abandoned := await self._store.abandon_open_turn(session_id)) is not None:
            logger.warning("Claude chat session %s adopted with turn %s still open", session_id, abandoned)
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
                # `TaskGroup` rather than bare `create_task`: both helpers run for exactly
                # this block's lifetime, and it owns awaiting and cancelling them. The
                # previous hand-rolled cancel/await/suppress dance had to remember to swallow
                # `CancelledError` and to log rather than raise, or cleanup was skipped.
                async with asyncio.TaskGroup() as helpers:
                    # The operator's abort lands on whichever replica the Service picks, which
                    # is rarely the one holding this websocket — so the event is driven by
                    # NOTIFY, not by a caller reaching into this process.
                    abort_watch = helpers.create_task(self._watch_aborts(session_id, abort_event))
                    # Says "this replica is still here" for as long as it is. Its absence is
                    # what another replica reclaims the session by; see `expire_stale_leases`.
                    renewal = helpers.create_task(self._renew_lease(session_id))
                    try:
                        await client.connect()
                        # One stream for the session, not one per turn. `receive_response()`
                        # is request-scoped — it assumes this process issued the turn and ends
                        # at that turn's `result` — which is wrong in two directions we now
                        # care about: a prompt folded into a running turn is answered inside
                        # the same response with no second `result`, and an adopted mid-flight
                        # turn was issued by a replica that is gone
                        # (<../../plans/cli_protocol_ownership.md>). Consuming a session-scoped
                        # stream and dispatching by frame makes the turn a bracket over it
                        # rather than a request/response pair.
                        frames = client.frames().__aiter__()
                        while True:
                            status = await self._store.status(session_id)
                            if status is None or status in ENDED_SESSION_STATUSES:
                                break
                            turn = await self._store.next_prompt(session_id)
                            if turn is None:
                                # Wait for a LISTEN/NOTIFY instead of polling.
                                await self._notifications.wait(ChatEventKind.PROMPT, session_id, timeout_seconds=30.0)
                                continue
                            # Cleared before the turn rather than after it: an abort notified
                            # just as the previous turn ended would otherwise sit set through
                            # the idle wait and kill the next turn on arrival. The remaining
                            # window is a notify racing these few statements, and `request_abort`
                            # no longer opens it from the other side — an abort is refused
                            # unless a turn is actually open.
                            abort_event.clear()
                            try:
                                await self._run_turn(
                                    client,
                                    frames,
                                    session_id,
                                    turn.prompt,
                                    turn_id=turn.turn_id,
                                    room_id=room_id,
                                    abort_event=abort_event,
                                )
                            except Exception as error:
                                logger.exception("Claude chat turn failed for session %s", session_id)
                                await self._store.fail(session_id, str(error))
                                break
                    finally:
                        # The helpers outlive the loop by construction, so ending it is what
                        # ends them; the group then awaits both before leaving this block.
                        abort_watch.cancel()
                        renewal.cancel()
            except* WebSocketDisconnect:
                # The runner went away, which is no longer the same as the session being over:
                # it keeps the CLI alive across a lost socket and redials. Hand the session back
                # and let the lease decide — a runner that returns is admitted, and one that
                # never does lets the grant lapse into the sweep.
                logger.info("Claude chat session %s lost its runner; leaving it for adoption", session_id)
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
            # about the session. It used to be recorded as a failure, which is what made every
            # console roll end every conversation: the row went terminal, so the runner's
            # reconnect was refused as `TERMINAL` and the supervisor built a replacement.
            #
            # Hand it back instead. The sandbox outlives this process, the runner redials, and
            # whichever replica answers adopts it. Nothing is swallowed: the grant `release_lease`
            # leaves is short, and the sweep fails the session if no runner returns.
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
        """Hold *session_id*'s lease for as long as this replica is running it."""
        while True:
            await self._store.renew_lease(session_id)
            await asyncio.sleep(LEASE_RENEW_INTERVAL.total_seconds())

    async def _watch_aborts(self, session_id: UUID, abort_event: asyncio.Event) -> None:
        """Set *abort_event* every time this session is told to abort, until cancelled.

        The operator's abort lands on whichever replica the Service picks, which is rarely
        the one holding this session's websocket, so it arrives over NOTIFY rather than by a
        caller reaching into this process.
        """
        async with self._notifications.subscribe(ChatEventKind.ABORT, session_id) as notified:
            while True:
                await notified.wait()
                notified.clear()
                abort_event.set()

    async def _run_turn(
        self,
        client: ClaudeCli,
        frames: AsyncIterator[dict[str, Any]],
        session_id: UUID,
        prompt: str,
        *,
        turn_id: UUID,
        room_id: str | None,
        abort_event: asyncio.Event,
    ) -> None:
        """Send *prompt* and consume the session's stream until this turn's `result`.

        *frames* belongs to the session, not to this call — see `handle_runner`. *turn_id* is the
        turn `next_prompt` opened for this prompt: this call is its span, so it closes it on
        every exit and is the only thing that does. A turn left open is therefore not a
        bookkeeping leak — it means no code got to close it, which is what a replica losing its
        pod mid-exchange looks like from outside.
        """
        await client.query(prompt)
        assistant_id: UUID | None = None
        streamed = ""
        saw_assistant_message = False
        result: dict[str, Any] | None = None
        # Whether anything has been said into the room yet, so the turn's final text is not
        # posted a second time: `result.result` normally repeats the last assistant message.
        spoke = False
        status = self._turn_status(room_id)
        status.start()
        aborted = asyncio.ensure_future(abort_event.wait())
        try:
            while True:
                # Exactly one `anext` in flight at a time, and the abort path consumes the one it
                # finds rather than starting another: `frames` is an async generator, which
                # refuses to be advanced twice at once ("anext(): asynchronous generator is
                # already running"), and an abort always arrives while this call is parked here.
                # Draining through a fresh `async for` therefore raised, so every mid-turn abort
                # failed the whole session instead of ending its turn.
                next_frame = asyncio.ensure_future(anext(frames))
                await asyncio.wait([next_frame, aborted], return_when=asyncio.FIRST_COMPLETED)
                if abort_event.is_set():
                    with contextlib.suppress(Exception):
                        await client.interrupt()
                    # Drain to this turn's end, beginning with the frame already asked for. The
                    # stream stays open for the next turn: it is the session's, so an interrupt
                    # ends a turn rather than the conversation.
                    while True:
                        remaining = await next_frame
                        if remaining.get("type") == "result":
                            result = remaining
                            break
                        next_frame = asyncio.ensure_future(anext(frames))
                    break
                # Not aborted, so `asyncio.wait` returned because the frame arrived.
                frame = next_frame.result()
                status.note(frame)
                match frame.get("type"):
                    case "stream_event":
                        if not (delta := _text_delta(frame.get("event", {}))):
                            continue
                        if assistant_id is None:
                            assistant_id = await self._store.begin_assistant(session_id)
                        streamed += delta
                        await self._store.update_assistant(session_id, assistant_id, streamed)
                        # The rollout keeps no deltas, so without this the text an interrupted
                        # turn produced would exist only in the message row and the log would
                        # simply stop mid-answer (R5.5b).
                        await self._store.update_partial_frame(session_id, streamed)
                    case "assistant":
                        saw_assistant_message = True
                        if assistant_id is None:
                            assistant_id = await self._store.begin_assistant(session_id)
                        blocks = _content_blocks(frame)
                        text = "".join(
                            str(block.get("text", "")) for block in blocks if block.get("type") == "text"
                        ).strip()
                        tool_uses = [
                            {"tool_use_id": block["id"], "name": block["name"], "input": block["input"]}
                            for block in blocks
                            if block.get("type") == "tool_use"
                        ]
                        said = text or streamed.strip()
                        await self._store.update_assistant(
                            session_id,
                            assistant_id,
                            said,
                            tool_uses=tool_uses,
                            # The wire's own id for this message, which is what lets a reader find
                            # its calls in the frame log rather than match them by position.
                            agent_message_id=_agent_message_id(frame),
                            complete=True,
                        )
                        # The real frame is already in the log — the recorder wrote it when
                        # the socket delivered it — so the stand-in has nothing to stand for.
                        await self._store.clear_partial_frame(session_id)
                        assistant_id = None
                        streamed = ""
                        # Speak each message as it finishes rather than only the final answer
                        # (R11.1). A turn that says what it is about to do, works, and then
                        # reports back is three messages in the transcript and used to be one in
                        # the room — so the room saw the conclusion and never the reasoning.
                        if said:
                            await self._deliver_reply(session_id, room_id, said)
                            spoke = True
                    case "result":
                        result = frame
                        break
            if result is None:
                raise RuntimeError("the Claude stream ended without a result for this turn")
            if result.get("is_error") and not abort_event.is_set():
                raise RuntimeError(
                    f"Claude returned {result.get('subtype')}: {result.get('stop_reason') or 'unknown error'}"
                )
            final_text = streamed.strip() or str(result.get("result") or "").strip()
            if abort_event.is_set():
                final_text += f"\n\n{ABORTED_NOTICE}"
            if assistant_id is not None:
                # A stream no completed frame closed. Its `partial` frame stays exactly as the
                # last delta left it: the rollout should show a turn that stopped mid-answer as
                # having stopped mid-answer. `final_text` is not written over it, because the
                # harness adds `[aborted by operator]` to that and the frame records what the
                # agent produced, not what the room was told.
                await self._store.update_assistant(session_id, assistant_id, final_text, tool_uses=[], complete=True)
                assistant_id = None
            elif not saw_assistant_message:
                assistant_id = await self._store.begin_assistant(session_id)
                await self._store.update_assistant(session_id, assistant_id, final_text, tool_uses=[], complete=True)
                assistant_id = None
            # Closed with what the `result` frame reported, which is the only place a turn's
            # cost, usage and duration exist — the console used to read `is_error` off that frame
            # and drop the rest for want of anywhere to put it.
            await self._store.end_turn(
                turn_id, TurnOutcome.ABORTED if abort_event.is_set() else TurnOutcome.ANSWERED, result
            )
            # Only what the room has not already heard. Each assistant message was spoken as it
            # finished, and `result.result` normally repeats the last of them — so delivering
            # `final_text` unconditionally would post the answer twice. Two cases still need it:
            # a turn whose text arrived only on the `result` frame (no assistant message ever
            # completed), and an abort, whose notice is on `final_text` and not on any message.
            if not spoke:
                await self._deliver_reply(session_id, room_id, final_text)
            elif abort_event.is_set():
                await self._deliver_reply(session_id, room_id, ABORTED_NOTICE)
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

    async def _deliver_reply(self, session_id: UUID, room_id: str | None, text: str) -> None:
        """Say *text* into the room, if this session serves one.

        Called for each assistant message as it finishes and once more at the turn's end for
        whatever the room has not heard yet. A session with no room needs nothing here: the
        SPA's client reads the message rows the turn already wrote.

        Deliberately not fatal: the message row is written before this runs, so a failed push
        is a delivery problem rather than a session problem. Failing here would mark the
        session dead and cost the whole conversation over a transient send error.
        TODO(matrix): retry rather than only logging, once the Matrix surface is the only
        one — today the message row is still readable in the SPA.
        """
        if self._room_surface is None or room_id is None:
            return
        try:
            await self._room_surface.deliver(room_id, text)
        except Exception:
            logger.exception("Reply delivery failed for session %s", session_id)

    async def aclose(self) -> None:
        await self._claims.aclose()


def _agent_message_id(frame: dict[str, Any]) -> str | None:
    """The agent's own id for an `assistant` frame's message, if it carried one."""
    message = frame.get("message")
    return str(agent_id) if isinstance(message, dict) and (agent_id := message.get("id")) else None


def _content_blocks(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """The content blocks of an `assistant` frame, or none if it carries none.

    Tolerant rather than strict: this reads the wire, where a block type we have never seen is
    a new CLI feature and not a bug in us. The frame itself is already recorded verbatim, so
    anything skipped here is still in the rollout.
    """
    message = frame.get("message")
    if not isinstance(message, dict):
        return []
    return [block for block in message.get("content", []) if isinstance(block, dict)]


def _text_delta(event: dict[str, Any]) -> str:
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


@dataclass(frozen=True, slots=True)
class _RolloutCalls:
    """What one session's frame log says about tool calls.

    Two indexes over the same frames, because the transcript joins to them by different keys: an
    assistant message finds its own calls by the agent's message id, and a call finds its answer by
    its own id — unique within a session, so that half needs no per-message association at all.
    """

    by_message: Mapping[str, list[dict[str, Any]]]
    results: Mapping[str, ClaudeChatToolResultView]


async def _rollout_calls(db: AsyncSession, session_id: UUID) -> _RolloutCalls:
    """Read the calls and their results out of the session's rollout.

    Both live only here: `assistant` frames carry the `tool_use` blocks, `user` frames carry the
    `tool_result` blocks the turn loop drops, and `claude_chat_messages.tool_uses` is a copy of the
    first half with the second half missing.
    """
    frames = await db.execute(
        select(ClaudeChatFrame.kind, ClaudeChatFrame.payload)
        .where(
            ClaudeChatFrame.session_id == session_id,
            ClaudeChatFrame.kind.in_([ChatMessageRole.ASSISTANT, ChatMessageRole.USER]),
        )
        .order_by(ClaudeChatFrame.frame_seq)
    )
    by_message: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, ClaudeChatToolResultView] = {}
    for kind, payload in frames:
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        agent_id = message.get("id")
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            match block.get("type"):
                case "tool_use" if kind == ChatMessageRole.ASSISTANT and agent_id:
                    by_message.setdefault(str(agent_id), []).append(
                        {"tool_use_id": block["id"], "name": block["name"], "input": block["input"]}
                    )
                case "tool_result" if call_id := block.get("tool_use_id"):
                    results[str(call_id)] = ClaudeChatToolResultView(
                        content=block.get("content"), is_error=bool(block.get("is_error"))
                    )
    return _RolloutCalls(by_message=by_message, results=results)


_NO_CALLS = _RolloutCalls(by_message=MappingProxyType({}), results=MappingProxyType({}))


def _message_view(message: ClaudeChatMessage, calls: _RolloutCalls = _NO_CALLS) -> ClaudeChatMessageView:
    # The rollout where the row points into it, the column otherwise. That column is the lossy copy
    # — the calls without their answers — and is kept only for rows with nothing to point at: ones
    # that predate the pointer, and ones this console synthesized rather than observed.
    recorded = calls.by_message.get(message.agent_message_id or "")
    return ClaudeChatMessageView(
        message_id=message.message_id,
        role=message.role,
        status=message.status,
        content=message.content,
        tool_uses=[
            ClaudeChatToolUseView.model_validate(tool_use | {"result": calls.results.get(tool_use["tool_use_id"])})
            for tool_use in (recorded if recorded is not None else message.tool_uses)
        ],
        error=message.error,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


async def _queued_prompt(db: AsyncSession, session_id: UUID, *, lock: bool = False) -> ClaudeChatPrompt | None:
    """The prompt *session_id* is waiting to run, if it has one.

    `SKIP LOCKED` when claiming, so two replicas racing on one session take different rows rather
    than blocking on each other — though a partial unique index means there is at most one to take.
    """
    query = (
        select(ClaudeChatPrompt)
        .where(ClaudeChatPrompt.session_id == session_id, ClaudeChatPrompt.claimed_at.is_(None))
        .order_by(ClaudeChatPrompt.queued_at)
    )
    prompt: ClaudeChatPrompt | None = await db.scalar(query.with_for_update(skip_locked=True) if lock else query)
    return prompt


async def _legacy_pending(db: AsyncSession, session_id: UUID, *, lock: bool = False) -> ClaudeChatMessage | None:
    """A prompt accepted by a replica on the previous image, which wrote no queue row.

    CLEANUP(added 2026-08-13): Remove once every pod runs an image with `claude_chat_prompts`
    (0033) — one roll after it ships. Until then this is the only way such a prompt is answered.
    """
    query = (
        select(ClaudeChatMessage)
        .where(
            ClaudeChatMessage.session_id == session_id,
            ClaudeChatMessage.role == ChatMessageRole.USER,
            ClaudeChatMessage.status == ChatMessageStatus.PENDING,
        )
        .order_by(ClaudeChatMessage.created_at)
    )
    message: ClaudeChatMessage | None = await db.scalar(query.with_for_update(skip_locked=True) if lock else query)
    return message


async def _open_turn(db: AsyncSession, session_id: UUID) -> UUID | None:
    """The turn *session_id* is in the middle of, if it is in the middle of one.

    The one question three things used to ask of `status == 'responding'`: whether a prompt may
    be accepted, whether there is anything to abort, and what the SPA should be told. A partial
    unique index makes "at most one" a schema property, so this is a lookup rather than a scan
    with a rule attached.
    """
    turn_id: UUID | None = await db.scalar(
        select(ClaudeChatTurn.turn_id).where(ClaudeChatTurn.session_id == session_id, ClaudeChatTurn.ended_at.is_(None))
    )
    return turn_id


async def _prompt_left(db: AsyncSession, session_id: UUID, first_frame_seq: int) -> bool:
    """Whether the turn starting at *first_frame_seq* ever wrote its prompt to the agent.

    **The console's own write is the evidence, not the CLI's acknowledgement.** `sent()` records
    the frame after `channel.write` returns, so its absence means the bytes did not go out; its
    presence means they did, and from then on the CLI's `command_lifecycle` — the only thing that
    would say whether the *CLI* has the prompt — may still be sitting in the runner's replay
    window, unrecorded, because replay does not begin until the socket is accepted and this runs
    before that. Asking a question the record cannot yet answer would re-ask a prompt the agent
    already has, which is the worse of the two failures: a duplicate turn instead of a lost one.

    So the ambiguous middle — written to a socket that then died — is deliberately treated as
    delivered, and what this closes is the window where nothing was written at all.
    """
    written = await db.scalar(
        select(ClaudeChatFrame.frame_seq)
        .where(
            ClaudeChatFrame.session_id == session_id,
            ClaudeChatFrame.frame_seq >= first_frame_seq,
            ClaudeChatFrame.direction == FrameDirection.TO_AGENT,
            ClaudeChatFrame.kind == PROMPT_FRAME_KIND,
        )
        .limit(1)
    )
    return written is not None


async def _requeue(db: AsyncSession, turn_id: UUID) -> None:
    """Put the prompts *turn_id* claimed back where `next_prompt` will find them again.

    Three writes because the claim is recorded in three places, and a prompt left in any of them
    is one the queue no longer offers: the queue row's `claimed_at`, the transcript row's status,
    and the link saying this turn answered it — which has to go, or the turn that finally does
    answer cannot record that it did (`(turn_id, message_id)` is the primary key, and the message
    half of it would repeat).
    """
    message_ids = list(
        (await db.scalars(select(ClaudeChatTurnPrompt.message_id).where(ClaudeChatTurnPrompt.turn_id == turn_id))).all()
    )
    if not message_ids:
        return
    now = datetime.now(UTC)
    for message in await db.scalars(select(ClaudeChatMessage).where(ClaudeChatMessage.message_id.in_(message_ids))):
        message.status = ChatMessageStatus.PENDING
        message.updated_at = now
    for prompt in await db.scalars(select(ClaudeChatPrompt).where(ClaudeChatPrompt.message_id.in_(message_ids))):
        prompt.claimed_at = None
    await db.execute(delete(ClaudeChatTurnPrompt).where(ClaudeChatTurnPrompt.turn_id == turn_id))
    logger.warning("Claude chat turn %s never asked its prompt; re-queued %d", turn_id, len(message_ids))


def _session_view(
    record: ClaudeChatSession, messages: list[ClaudeChatMessage], *, responding: bool, calls: _RolloutCalls = _NO_CALLS
) -> ClaudeChatSessionView:
    """The session as the SPA reads it, with `responding` derived from an open turn.

    `status` is the frontend's contract (`frontend/x/claude_chat_page.tsx` switches on it), so
    the column underneath can stop carrying turn state without a frontend release. A live
    session with a turn in flight reports `responding`; the session's own lifecycle —
    provisioning, closing, closed, failed — always wins, because a turn left open by a replica
    that died says nothing about a session the sweep has since failed.

    The `record.status == RESPONDING` arm is the roll's other half: a replica on the previous
    image still writes that column, and its sessions have no turn rows to derive from.
    """
    live = record.status in {ChatSessionStatus.READY, ChatSessionStatus.RESPONDING}
    status = (
        ChatSessionStatus.RESPONDING
        if live and (responding or record.status == ChatSessionStatus.RESPONDING)
        else record.status
    )
    return ClaudeChatSessionView(
        session_id=record.session_id,
        status=status,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        provisioning=None,
        messages=[_message_view(message, calls) for message in messages],
    )


def _provisioning_view(claim_name: str, *, step: ProvisioningStep, **values: Any) -> ClaudeSandboxProvisioningView:
    return ClaudeSandboxProvisioningView(claim_name=claim_name, step=step, inspected_at=datetime.now(UTC), **values)


def _condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    conditions = resource.get("status", {}).get("conditions", []) or []
    return next(
        (
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == condition_type
        ),
        None,
    )


def _condition_text(condition: dict[str, Any] | None, key: str) -> str | None:
    if condition is None:
        return None
    value = condition.get(key)
    return value if isinstance(value, str) and value else None


def _condition_bool(condition: dict[str, Any] | None) -> bool | None:
    status = _condition_text(condition, "status")
    if status == "True":
        return True
    if status == "False":
        return False
    return None


def _nested_string(resource: dict[str, Any], *path: str) -> str | None:
    value: Any = resource
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) and value else None


def _pod_ready(pod: k8s_client.V1Pod) -> bool | None:
    if pod.status is None or pod.status.conditions is None:
        return None
    condition = next((item for item in pod.status.conditions if item.type == "Ready"), None)
    if condition is None:
        return None
    if condition.status == "True":
        return True
    if condition.status == "False":
        return False
    return None


def _container_status(pod: k8s_client.V1Pod, name: str) -> tuple[bool | None, str | None]:
    if pod.status is None or pod.status.container_statuses is None:
        return None, None
    status = next((item for item in pod.status.container_statuses if item.name == name), None)
    if status is None:
        return None, None
    state = status.state
    if state is None:
        return status.ready, None
    if state.running is not None:
        detail = "running"
    elif state.waiting is not None:
        reason = state.waiting.reason or "unknown"
        detail = f"waiting: {reason}"
    elif state.terminated is not None:
        reason = state.terminated.reason or f"exit {state.terminated.exit_code}"
        detail = f"terminated: {reason}"
    else:
        detail = None
    return status.ready, detail


def _service(request: Request) -> ClaudeChatService:
    service = cast(ClaudeChatService | None, request.app.state.claude_chat_service)
    if service is None:
        raise HTTPException(status_code=503, detail="sandbox Claude chat is not configured")
    return service


def _store(request: Request) -> ClaudeChatStore:
    store = cast(ClaudeChatStore | None, request.app.state.claude_chat_store)
    if store is None:
        raise HTTPException(status_code=503, detail="sandbox Claude chat is not configured")
    return store


def _notifications(request: Request) -> ChatNotifications:
    notifications = cast(ChatNotifications | None, request.app.state.claude_chat_notifications)
    if notifications is None:
        raise HTTPException(status_code=503, detail="Claude chat runtime is not configured")
    return notifications


ChatNotificationsDep = Annotated[ChatNotifications, Depends(_notifications)]
ClaudeChatServiceDep = Annotated[ClaudeChatService, Depends(_service)]
ClaudeChatStoreDep = Annotated[ClaudeChatStore, Depends(_store)]


@router.post("/api/claude/sessions")
async def create_session(actor: OperatorActorDep, service: ClaudeChatServiceDep) -> ClaudeChatSessionView:
    try:
        return await service.create(actor.operator_id, SpaSession())
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/api/claude/sessions/{session_id}")
async def get_session(
    session_id: UUID, actor: OperatorActorDep, service: ClaudeChatServiceDep
) -> ClaudeChatSessionView:
    try:
        return await service.get(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Claude chat session not found") from error


async def _sse_stream(
    store: ClaudeChatStore, notifications: ChatNotifications, operator_id: UUID, session_id: UUID
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
        if last_status in {ChatSessionStatus.CLOSED, ChatSessionStatus.FAILED}:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        await notifications.wait(ChatEventKind.UPDATE, session_id, timeout_seconds=30.0)
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


@router.get("/api/claude/sessions/{session_id}/stream")
async def stream_session(
    session_id: UUID, actor: OperatorActorDep, store: ClaudeChatStoreDep, notifications: ChatNotificationsDep
) -> StreamingResponse:
    if not await store.session_exists(actor.operator_id, session_id):
        raise HTTPException(status_code=404, detail="Claude chat session not found")
    return StreamingResponse(
        _sse_stream(store, notifications, actor.operator_id, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/claude/sessions/{session_id}/abort", status_code=202)
async def abort_session(session_id: UUID, actor: OperatorActorDep, service: ClaudeChatServiceDep) -> dict[str, str]:
    try:
        aborted = await service.request_abort(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Claude chat session not found") from error
    if not aborted:
        raise HTTPException(status_code=409, detail="no active turn to abort")
    return {"status": "aborted"}


@router.post("/api/claude/sessions/{session_id}/messages")
async def send_message(
    session_id: UUID, body: ClaudeChatPromptRequest, actor: OperatorActorDep, store: ClaudeChatStoreDep
) -> ClaudeChatMessageView:
    try:
        return await store.enqueue_prompt(actor.operator_id, session_id, body.text)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Claude chat session not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/api/claude/sessions/{session_id}", status_code=204)
async def delete_session(session_id: UUID, actor: OperatorActorDep, service: ClaudeChatServiceDep) -> None:
    try:
        await service.dispose(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Claude chat session not found") from error


@internal_router.websocket("/internal/claude/runner/{session_id}")
async def runner_websocket(websocket: WebSocket, session_id: UUID) -> None:
    service = cast(ClaudeChatService | None, websocket.app.state.claude_chat_service)
    authorization = websocket.headers.get("authorization", "")
    scheme, _, bearer = authorization.partition(" ")
    if service is None or scheme.lower() != "bearer" or not bearer:
        await websocket.close(code=NOT_ADMITTED_CODE, reason="runner authentication required")
        return
    await service.handle_runner(websocket, session_id, bearer)
