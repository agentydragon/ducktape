"""Operator chat sessions backed by Claude Code in Agent Sandbox pods."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Protocol, cast
from uuid import UUID, uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent, SystemPromptPreset
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    ENDED_SESSION_STATUSES,
    LIVE_SESSION_STATUSES,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSessionStatus,
    ChatSurface,
    FrameDirection,
)
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ClaudeChatFrame, ClaudeChatMessage, ClaudeChatSession
from haku.console.operator_auth import OperatorActorDep
from haku.console.x.chat_notifications import ChatEventKind, ChatNotifications, notify
from haku.runtime.x.agent_sdk_transport.options import build_claude_launch, enable_fine_grained_streaming
from haku.runtime.x.agent_sdk_transport.protocol import (
    CONSOLE_TO_RUNNER,
    RUNNER_TO_CONSOLE,
    ClaudeMessage,
    ConsoleToRunner,
    RunnerToConsole,
    TextWebSocket,
)
from haku.runtime.x.agent_sdk_transport.transport import WebSocketTransport

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


class ClaudeChatToolUseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    name: str
    input: dict[str, Any]


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


class ClaudeChatPrompt(BaseModel):
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


@dataclass(frozen=True)
class MatrixSession:
    """A session created to serve one Matrix room, which it records for good.

    Carried as a variant rather than a `surface` enum beside an optional `room_id`, because
    the two combinations that pair would also admit — a Matrix session with no room, a room on
    an SPA session — are states no caller could act on. The table repeats the rule as a pair of
    check constraints, since the columns outlive this call signature.
    """

    room_id: str


SessionSurface = SpaSession | MatrixSession


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
                    surface=ChatSurface.MATRIX if isinstance(surface, MatrixSession) else ChatSurface.SPA,
                    room_id=surface.room_id if isinstance(surface, MatrixSession) else None,
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
            return _session_view(record, messages)

    async def authenticate_bridge(self, session_id: UUID, token: str) -> BridgeAuthentication:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            record = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if record is None or not secrets.compare_digest(record.bridge_token_fingerprint, self._fingerprint(token)):
                return BridgeAuthentication.REJECTED
            if record.status in ENDED_SESSION_STATUSES:
                return BridgeAuthentication.TERMINAL
            if record.status != ChatSessionStatus.PROVISIONING or record.bridge_connected_at is not None:
                return BridgeAuthentication.REJECTED
            record.bridge_connected_at = now
            record.status = ChatSessionStatus.READY
            record.updated_at = now
            return BridgeAuthentication.ACCEPTED

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
            existing = await db.scalar(
                select(ClaudeChatMessage).where(
                    ClaudeChatMessage.session_id == session_id, ClaudeChatMessage.status == ChatMessageStatus.PENDING
                )
            )
            if existing is not None:
                raise RuntimeError("a prompt is already queued")
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
            chat.status = ChatSessionStatus.RESPONDING
            chat.updated_at = now
            await notify(db, ChatEventKind.PROMPT, session_id)
            await notify(db, ChatEventKind.UPDATE, session_id)
        return _message_view(message)

    async def next_prompt(self, session_id: UUID) -> tuple[UUID, str] | None:
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return None
            message = await db.scalar(
                select(ClaudeChatMessage)
                .where(
                    ClaudeChatMessage.session_id == session_id,
                    ClaudeChatMessage.role == ChatMessageRole.USER,
                    ClaudeChatMessage.status == ChatMessageStatus.PENDING,
                )
                .order_by(ClaudeChatMessage.created_at)
                .with_for_update(skip_locked=True)
            )
            if message is None:
                return None
            now = datetime.now(UTC)
            message.status = ChatMessageStatus.COMPLETE
            message.updated_at = now
            chat.status = ChatSessionStatus.RESPONDING
            chat.updated_at = now
            return message.message_id, message.content

    async def record_frame(self, session_id: UUID, direction: FrameDirection, payload: dict[str, Any]) -> None:
        """Append one protocol frame to the session's rollout.

        Failures are not swallowed. Every other write in a turn reaches the same database, so
        one that cannot record has already lost the session — and a rollout with quiet holes is
        exactly the record that looks complete while being wrong, which is what this table
        exists to stop.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            db.add(
                ClaudeChatFrame(
                    session_id=session_id,
                    direction=direction,
                    kind=_frame_kind(payload),
                    payload=payload,
                    partial=False,
                    created_at=now,
                    updated_at=now,
                )
            )

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
            message.status = ChatMessageStatus.COMPLETE if complete else ChatMessageStatus.STREAMING
            message.updated_at = now
            if chat.status not in ENDED_SESSION_STATUSES:
                chat.status = ChatSessionStatus.RESPONDING
            chat.updated_at = now
            await notify(db, ChatEventKind.UPDATE, session_id)

    async def complete_turn(self, session_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return
            chat.status = ChatSessionStatus.READY
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

    async def status(self, session_id: UUID) -> ChatSessionStatus | None:
        async with self._sessions() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            return chat.status if chat is not None else None

    async def renew_lease(self, session_id: UUID) -> None:
        """Assert that this replica still holds *session_id* and is still working on it."""
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is not None and chat.status in LIVE_SESSION_STATUSES:
                chat.lease_expires_at = datetime.now(UTC) + LEASE_TTL

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
                logger.error("Claude chat session %s lease expired; its console replica is gone", session_id)
                chat.status = ChatSessionStatus.FAILED
                chat.error = "console replica holding this session went away mid-turn"
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
            status = await db.scalar(select(ClaudeChatSession.status).where(ClaudeChatSession.session_id == session_id))
            if status != ChatSessionStatus.RESPONDING:
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


def _assistant_frame(text: str) -> dict[str, Any]:
    """The frame shape the agent will send, for the one the console stands in for meanwhile.

    Same shape as the wire's, so a reader needs no second case; the row's `partial` column is
    what says it was reconstructed rather than observed.
    """
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _frame_kind(payload: dict[str, Any]) -> str:
    kind = payload.get("type")
    if not isinstance(kind, str):
        raise ValueError(f"protocol frame has no type: {payload=}")
    return kind


class RecordingWebSocket(TextWebSocket):
    """Persists the rollout by watching the socket the transport already talks over.

    A decorator rather than a hook inside `WebSocketTransport`, because which frames crossed
    is visible from here and the transport is shared code that should not learn about the
    console's database. The cost is re-decoding each frame's envelope, which is one `json.loads`
    against a turn's worth of model inference.
    """

    def __init__(self, inner: TextWebSocket, store: ClaudeChatStore, session_id: UUID):
        self._inner = inner
        self._store = store
        self._session_id = session_id

    async def send_text(self, data: str) -> None:
        await self._inner.send_text(data)
        await self._record(CONSOLE_TO_RUNNER.validate_json(data), FrameDirection.TO_AGENT)

    async def receive_text(self) -> str:
        data = await self._inner.receive_text()
        await self._record(RUNNER_TO_CONSOLE.validate_json(data), FrameDirection.FROM_AGENT)
        return data

    async def close(self) -> None:
        await self._inner.close()

    async def _record(self, frame: ConsoleToRunner | RunnerToConsole, direction: FrameDirection) -> None:
        if isinstance(frame, ClaudeMessage) and _frame_kind(frame.payload) != _PARTIAL_FRAME_KIND:
            await self._store.record_frame(self._session_id, direction, frame.payload)


# Receives a finished turn's answer, in addition to the message row the SPA reads. The
# Matrix surface has no SSE stream to read from, so a completed turn has to be pushed
# somewhere; see `haku.console.x.matrix_session.MatrixReplySink`, which filters to the one
# session it owns. Unset means the rows are the only output, which is the console SPA.
ReplySink = Callable[[UUID, str], Awaitable[None]]

# Supplies the system prompt a session starts with, or `None` for a session this source does
# not speak for. Same shape and same reason as `ReplySink`: one service serves both the
# Matrix room and the console SPA, and only the Matrix surface has a room to describe — see
# `haku.console.x.matrix_session.MatrixSystemPrompt`. Unset, or `None` for a given session,
# leaves the Claude Code preset alone, which is what the SPA has always run with.
SystemPromptSource = Callable[[UUID], Awaitable[str | None]]

# Receives one sandbox progress report for a session. Same shape and same filtering duty as
# `ReplySink` — see `haku.console.x.matrix_session.MatrixProgressSink`. Unset means the
# reports are logged and go no further, which is the console SPA's behaviour.
ProgressSink = Callable[[UUID, str], Awaitable[None]]


class ClaudeChatService:
    def __init__(
        self,
        config: ClaudeRuntimeConfig,
        store: ClaudeChatStore,
        claims: SandboxClaims,
        notifications: ChatNotifications,
        *,
        mcp_token: SecretStr,
        reply_sink: ReplySink | None = None,
        system_prompt: SystemPromptSource | None = None,
        progress_sink: ProgressSink | None = None,
    ):
        self._config = config
        self._store = store
        self._claims = claims
        self._notifications = notifications
        self._mcp_token = mcp_token
        self._reply_sink = reply_sink
        self._system_prompt = system_prompt
        self._progress_sink = progress_sink

    async def request_abort(self, session_id: UUID) -> bool:
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

    async def _preset_with(self, session_id: UUID) -> SystemPromptPreset | None:
        """Claude Code's own system prompt, plus who this session is.

        `append` rather than a bare string, which would *replace* the preset: the built-ins
        (Read, Bash, Edit) are live in the sandbox and the preset is what tells the model how
        to drive them. Haku's identity is an addition to that, not a substitute for it.

        The literal is the SDK's own `SystemPromptPreset` TypedDict, so mypy checks its keys
        and the two `Literal` values against the pinned SDK rather than trusting this spelling.
        """
        if self._system_prompt is None or (rendered := await self._system_prompt(session_id)) is None:
            return None
        return {"type": "preset", "preset": "claude_code", "append": rendered}

    def _progress_reporter(self, session_id: UUID) -> Callable[[str], Awaitable[None]]:
        """Log every sandbox progress report, and pass it on if anything is listening."""

        async def report(detail: str) -> None:
            logger.info("Claude sandbox %s: %s", session_id, detail)
            if self._progress_sink is not None:
                await self._progress_sink(session_id, detail)

        return report

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == BridgeAuthentication.TERMINAL:
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1008, reason="runner session is already terminal")
            return
        if authentication == BridgeAuthentication.REJECTED:
            await websocket.close(code=1008, reason="invalid or consumed runner credential")
            return
        # Rendered before the socket is accepted, alongside the other admission failures, so a
        # broken prompt ends the session where the supervisor can see it (and say so in the
        # room) instead of raising past the cleanup below and leaving the claim stranded.
        # Failing is deliberate: a session that silently started without its identity is the
        # generic-assistant bug this prompt exists to fix, and it would be invisible.
        try:
            preset = await self._preset_with(session_id)
        except Exception as error:
            logger.exception("Claude system prompt failed to render for session %s", session_id)
            await self._store.fail(session_id, f"system prompt failed to render: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="system prompt failed to render")
            return
        await websocket.accept()
        adapter = RecordingWebSocket(StarletteTextWebSocket(websocket), self._store, session_id)
        options = enable_fine_grained_streaming(
            ClaudeAgentOptions(
                system_prompt=preset,
                cwd=self._config.cwd,
                env=self._config.claude_environment(),
                mcp_servers={
                    "haku-console": {
                        "type": "http",
                        "url": self._config.mcp_url,
                        "headers": {"Authorization": f"Bearer {self._mcp_token.get_secret_value()}"},
                    }
                },
                strict_mcp_config=True,
                permission_mode="bypassPermissions",
                setting_sources=[],
            )
        )
        client = ClaudeSDKClient(
            options=options,
            transport=WebSocketTransport(adapter, build_claude_launch(options), self._progress_reporter(session_id)),
        )
        abort_event = asyncio.Event()
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
                        while True:
                            status = await self._store.status(session_id)
                            if status is None or status in ENDED_SESSION_STATUSES:
                                break
                            prompt = await self._store.next_prompt(session_id)
                            if prompt is None:
                                # Wait for a LISTEN/NOTIFY instead of polling.
                                await self._notifications.wait(ChatEventKind.PROMPT, session_id, timeout_seconds=30.0)
                                continue
                            _, text = prompt
                            # Cleared here rather than after the turn: an abort notified just
                            # as the previous turn ended would otherwise sit set through the
                            # idle wait and kill the next turn on arrival. A notify racing the
                            # next few statements can still do that, which needs the abort to
                            # name a turn rather than a session.
                            abort_event.clear()
                            try:
                                await self._run_turn(client, session_id, text, abort_event=abort_event)
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
                await self._store.fail(session_id, "sandbox runner disconnected")
            except* Exception as errors:
                # `fail` records the message; the traceback is what says which call produced
                # it, and the listener mismatch was three frames below anything it named.
                logger.exception("Claude runtime failed for session %s", session_id)
                await self._store.fail(session_id, f"Claude runtime failed: {_first_message(errors)}")
        except asyncio.CancelledError:
            # `CancelledError` is a `BaseException`, so neither clause above sees it. This is
            # the shutdown path — a rolling update, an evicted pod — and leaving it unrecorded
            # is what let a room sit on "responding" forever: the status stayed live, and the
            # only process that could correct it was the one going away. Record, then re-raise;
            # cancellation is never swallowed.
            await self._store.fail(session_id, "console replica shut down mid-session")
            raise
        finally:
            # Shielded because everything here is an `await` and this task may already be
            # cancelled, in which case the first one would re-raise and the rest would silently
            # not happen — which is how `closed()` came to be skipped. Best effort even so: a
            # SIGKILL runs no finalizer at all, which is why the lease, not this block, is what
            # actually guarantees the session stops looking alive.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.shield(asyncio.wait_for(self._finalize(session_id, client), timeout=10))

    async def _finalize(self, session_id: UUID, client: ClaudeSDKClient) -> None:
        await client.disconnect()
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
        self, client: ClaudeSDKClient, session_id: UUID, prompt: str, *, abort_event: asyncio.Event
    ) -> None:
        await client.query(prompt)
        assistant_id: UUID | None = None
        streamed = ""
        saw_assistant_message = False
        result: ResultMessage | None = None
        try:
            response_iter = client.receive_response().__aiter__()
            while True:
                done, pending = await asyncio.wait(
                    [asyncio.ensure_future(response_iter.__anext__()), asyncio.ensure_future(abort_event.wait())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if abort_event.is_set():
                    with contextlib.suppress(Exception):
                        await client.interrupt()
                    # Drain remaining messages from the interrupted turn.
                    async for message in response_iter:
                        if isinstance(message, ResultMessage):
                            result = message
                            break
                    break
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        for t in pending:
                            t.cancel()
                        raise exc
                    msg = task.result()
                    if not isinstance(msg, (StreamEvent, AssistantMessage, ResultMessage)):
                        continue  # abort_event.wait() task — skip
                    if isinstance(msg, StreamEvent):
                        delta = _text_delta(msg.event)
                        if not delta:
                            continue
                        if assistant_id is None:
                            assistant_id = await self._store.begin_assistant(session_id)
                        streamed += delta
                        await self._store.update_assistant(session_id, assistant_id, streamed)
                        # The rollout keeps no deltas, so without this the text an interrupted
                        # turn produced would exist only in the message row and the log would
                        # simply stop mid-answer (R5.5b).
                        await self._store.update_partial_frame(session_id, streamed)
                    elif isinstance(msg, AssistantMessage):
                        saw_assistant_message = True
                        if assistant_id is None:
                            assistant_id = await self._store.begin_assistant(session_id)
                        text = "".join(block.text for block in msg.content if isinstance(block, TextBlock)).strip()
                        tool_uses = [
                            {"tool_use_id": block.id, "name": block.name, "input": block.input}
                            for block in msg.content
                            if isinstance(block, ToolUseBlock)
                        ]
                        await self._store.update_assistant(
                            session_id, assistant_id, text or streamed.strip(), tool_uses=tool_uses, complete=True
                        )
                        # The real frame is already in the log — the recorder wrote it when the
                        # socket delivered it — so the stand-in has nothing left to stand for.
                        await self._store.clear_partial_frame(session_id)
                        assistant_id = None
                        streamed = ""
                    elif isinstance(msg, ResultMessage):
                        result = msg
                for task in pending:
                    task.cancel()
                if result is not None:
                    break
            if result is None:
                raise RuntimeError("Claude response ended without a result")
            if result.is_error and not abort_event.is_set():
                raise RuntimeError(f"Claude returned {result.subtype}: {result.stop_reason or 'unknown error'}")
            final_text = streamed.strip() or (result.result or "").strip()
            if abort_event.is_set():
                final_text += "\n\n[aborted by operator]"
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
            await self._store.complete_turn(session_id)
            await self._deliver_reply(session_id, final_text)
        except Exception as error:
            if assistant_id is not None:
                await self._store.fail(session_id, str(error), assistant_id)
            raise

    async def _deliver_reply(self, session_id: UUID, final_text: str) -> None:
        """Push the answer to the reply sink, if one is attached.

        Deliberately after `complete_turn` and deliberately not fatal: the turn did happen
        and its row is written, so a failed push is a delivery problem, not a session
        problem. Failing here would mark the session dead and cost the whole conversation
        over a transient send error.
        TODO(matrix): retry rather than only logging, once the Matrix surface is the only
        one — today the message row is still readable in the SPA.
        """
        if self._reply_sink is None:
            return
        try:
            await self._reply_sink(session_id, final_text)
        except Exception:
            logger.exception("Reply delivery failed for session %s", session_id)

    async def aclose(self) -> None:
        await self._claims.aclose()


def _text_delta(event: dict[str, Any]) -> str:
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _message_view(message: ClaudeChatMessage) -> ClaudeChatMessageView:
    return ClaudeChatMessageView(
        message_id=message.message_id,
        role=message.role,
        status=message.status,
        content=message.content,
        tool_uses=[ClaudeChatToolUseView.model_validate(tool_use) for tool_use in message.tool_uses],
        error=message.error,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _session_view(record: ClaudeChatSession, messages: list[ClaudeChatMessage]) -> ClaudeChatSessionView:
    return ClaudeChatSessionView(
        session_id=record.session_id,
        status=record.status,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        provisioning=None,
        messages=[_message_view(message) for message in messages],
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
    yield f"data: {last_view.model_dump_json()}\n\n"
    while True:
        if last_view.status in {ChatSessionStatus.CLOSED, ChatSessionStatus.FAILED}:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        await notifications.wait(ChatEventKind.UPDATE, session_id, timeout_seconds=30.0)
        try:
            next_view = await store.get(operator_id, session_id)
        except KeyError:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        if next_view.model_dump_json() != last_view.model_dump_json():
            last_view = next_view
            yield f"data: {next_view.model_dump_json()}\n\n"


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
    if not await service._store.session_exists(actor.operator_id, session_id):
        raise HTTPException(status_code=404, detail="Claude chat session not found")
    if not await service.request_abort(session_id):
        raise HTTPException(status_code=409, detail="no active turn to abort")
    return {"status": "aborted"}


@router.post("/api/claude/sessions/{session_id}/messages")
async def send_message(
    session_id: UUID, body: ClaudeChatPrompt, actor: OperatorActorDep, store: ClaudeChatStoreDep
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
        await websocket.close(code=1008, reason="runner authentication required")
        return
    await service.handle_runner(websocket, session_id, bearer)
