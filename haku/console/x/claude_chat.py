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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ClaudeChatMessage, ClaudeChatSession
from haku.console.operator_auth import OperatorActorDep
from haku.runtime.agent_sdk_transport.options import build_claude_launch, enable_fine_grained_streaming
from haku.runtime.agent_sdk_transport.protocol import TextWebSocket
from haku.runtime.agent_sdk_transport.transport import WebSocketTransport

router = APIRouter(tags=["claude-chat"])
internal_router = APIRouter(tags=["claude-chat-internal"])
logger = logging.getLogger(__name__)

_PROMPT_CHANNEL = "claude_chat_prompt"
_UPDATE_CHANNEL = "claude_chat_update"

SessionStatus = Literal["provisioning", "ready", "responding", "closing", "closed", "failed"]
MessageRole = Literal["user", "assistant"]
MessageStatus = Literal["pending", "streaming", "complete", "failed"]
BridgeAuthentication = Literal["accepted", "terminal", "rejected"]


class ClaudeChatToolUseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    name: str
    input: dict[str, Any]


class ClaudeChatMessageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    role: MessageRole
    status: MessageStatus
    content: str
    tool_uses: list[ClaudeChatToolUseView]
    error: str | None
    created_at: datetime
    updated_at: datetime


ProvisioningStep = Literal[
    "claim_created", "waiting_for_sandbox", "waiting_for_pod", "waiting_for_pod_ready", "waiting_for_runner"
]


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
    status: SessionStatus
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
                return _provisioning_view(claim_name, step="claim_created")
            raise

        claim_condition = _condition(claim, "Ready")
        claim_reason = _condition_text(claim_condition, "reason")
        claim_message = _condition_text(claim_condition, "message")
        sandbox_name = _nested_string(claim, "status", "sandbox", "name")
        if sandbox_name is None:
            return _provisioning_view(
                claim_name,
                step="waiting_for_sandbox",
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
                    step="waiting_for_sandbox",
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
                    step="waiting_for_pod",
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
        step: ProvisioningStep = "waiting_for_runner" if pod_ready and runner_ready else "waiting_for_pod_ready"
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


class ClaudeChatStore:
    """Async Postgres store for Claude chat sessions."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine):
        self._sessions = sessions
        self._engine = engine

    @staticmethod
    def _fingerprint(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    @staticmethod
    async def _notify(db: AsyncSession, channel: str, session_id: UUID) -> None:
        """Emit ``pg_notify`` inside an active session transaction."""
        await db.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": str(session_id)})

    async def create(self, operator_id: UUID) -> tuple[ClaudeChatSessionView, str]:
        now = datetime.now(UTC)
        session_id = uuid4()
        bridge_token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as db:
            db.add(
                ClaudeChatSession(
                    session_id=session_id,
                    operator_id=operator_id,
                    status="provisioning",
                    bridge_token_fingerprint=self._fingerprint(bridge_token),
                    bridge_connected_at=None,
                    error=None,
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
                return "rejected"
            if record.status in {"closing", "closed", "failed"}:
                return "terminal"
            if record.status != "provisioning" or record.bridge_connected_at is not None:
                return "rejected"
            record.bridge_connected_at = now
            record.status = "ready"
            record.updated_at = now
            return "accepted"

    async def claim_cleanup_candidates(self) -> list[UUID]:
        """Return terminal sessions whose hashed rendezvous credential still marks cleanup pending."""
        async with self._sessions() as db:
            result = await db.scalars(
                select(ClaudeChatSession.session_id).where(
                    ClaudeChatSession.status.in_(("closing", "closed", "failed")),
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
            if chat.status == "closing":
                chat.status = "closed"
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
            if chat.status != "ready":
                raise RuntimeError(f"session is not ready (status={chat.status})")
            existing = await db.scalar(
                select(ClaudeChatMessage).where(
                    ClaudeChatMessage.session_id == session_id, ClaudeChatMessage.status == "pending"
                )
            )
            if existing is not None:
                raise RuntimeError("a prompt is already queued")
            message = ClaudeChatMessage(
                message_id=uuid4(),
                session_id=session_id,
                role="user",
                status="pending",
                content=prompt_text,
                tool_uses=[],
                error=None,
                created_at=now,
                updated_at=now,
            )
            db.add(message)
            chat.status = "responding"
            chat.updated_at = now
            await self._notify(db, _PROMPT_CHANNEL, session_id)
        return _message_view(message)

    async def next_prompt(self, session_id: UUID) -> tuple[UUID, str] | None:
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id, with_for_update=True)
            if chat is None or chat.status in {"closing", "closed", "failed"}:
                return None
            message = await db.scalar(
                select(ClaudeChatMessage)
                .where(
                    ClaudeChatMessage.session_id == session_id,
                    ClaudeChatMessage.role == "user",
                    ClaudeChatMessage.status == "pending",
                )
                .order_by(ClaudeChatMessage.created_at)
                .with_for_update(skip_locked=True)
            )
            if message is None:
                return None
            now = datetime.now(UTC)
            message.status = "complete"
            message.updated_at = now
            chat.status = "responding"
            chat.updated_at = now
            return message.message_id, message.content

    async def begin_assistant(self, session_id: UUID) -> UUID:
        now = datetime.now(UTC)
        message_id = uuid4()
        async with self._sessions.begin() as db:
            db.add(
                ClaudeChatMessage(
                    message_id=message_id,
                    session_id=session_id,
                    role="assistant",
                    status="streaming",
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
            message.status = "complete" if complete else "streaming"
            message.updated_at = now
            if chat.status not in {"closing", "closed", "failed"}:
                chat.status = "responding"
            chat.updated_at = now
            await self._notify(db, _UPDATE_CHANNEL, session_id)

    async def complete_turn(self, session_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is None or chat.status in {"closing", "closed", "failed"}:
                return
            chat.status = "ready"
            chat.updated_at = now

    async def fail(self, session_id: UUID, error: str, message_id: UUID | None = None) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is not None and chat.status not in {"closing", "closed"}:
                chat.status = "failed"
                chat.error = error
                chat.updated_at = now
            if message_id is not None:
                message = await db.get(ClaudeChatMessage, message_id)
                if message is not None:
                    message.status = "failed"
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
            chat.status = "closing"
            chat.updated_at = datetime.now(UTC)

    async def status(self, session_id: UUID) -> str | None:
        async with self._sessions() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            return chat.status if chat is not None else None

    async def closed(self, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.get(ClaudeChatSession, session_id)
            if chat is not None and chat.status != "failed":
                chat.status = "closed"
                chat.updated_at = datetime.now(UTC)
                await self._notify(db, _UPDATE_CHANNEL, session_id)

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

    async def _listen(self, channel: str, session_id: UUID, *, timeout_seconds: float) -> bool:
        """Block until a Postgres NOTIFY on *channel* fires for *session_id*.

        Uses a raw asyncpg connection from the engine's pool for LISTEN/NOTIFY. Listener failures
        propagate: this path is deliberately notification-only, so silently falling back to polling
        would hide a broken cross-replica update channel.
        """
        async with self._engine.connect() as conn:
            raw = await conn.get_raw_connection()
            assert raw.dbapi_connection is not None
            pg_conn = raw.dbapi_connection.driver_connection
            await pg_conn.set_autocommit(True)
            await pg_conn.execute(f"LISTEN {channel}")
            try:
                async with asyncio.timeout(timeout_seconds):
                    async for note in pg_conn.notifies():
                        if note.payload == str(session_id):
                            return True
                return False
            except TimeoutError:
                return False
            finally:
                await pg_conn.execute(f"UNLISTEN {channel}")

    async def listen_for_update(self, session_id: UUID, *, timeout_seconds: float = 30.0) -> None:
        """Wait for a LISTEN/NOTIFY on the update channel for this session."""
        await self._listen(_UPDATE_CHANNEL, session_id, timeout_seconds=timeout_seconds)

    async def wait_for_prompt(self, session_id: UUID, *, timeout_seconds: float = 30.0) -> bool:
        """Wait for a prompt NOTIFY. Returns True if a prompt notification arrived."""
        return await self._listen(_PROMPT_CHANNEL, session_id, timeout_seconds=timeout_seconds)


class StarletteTextWebSocket(TextWebSocket):
    def __init__(self, websocket: WebSocket):
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        await self._websocket.send_text(data)

    async def receive_text(self) -> str:
        return await self._websocket.receive_text()

    async def close(self) -> None:
        await self._websocket.close()


# Receives a finished turn's answer, in addition to the message row the SPA reads. The
# Matrix surface has no SSE stream to read from, so a completed turn has to be pushed
# somewhere; see `haku.console.x.matrix_session.MatrixReplySink`, which filters to the one
# session it owns. Unset means the rows are the only output, which is the console SPA.
ReplySink = Callable[[UUID, str], Awaitable[None]]


class ClaudeChatService:
    def __init__(
        self,
        config: ClaudeRuntimeConfig,
        store: ClaudeChatStore,
        claims: SandboxClaims,
        *,
        mcp_token: SecretStr,
        reply_sink: ReplySink | None = None,
    ):
        self._config = config
        self._store = store
        self._claims = claims
        self._mcp_token = mcp_token
        self._reply_sink = reply_sink
        self._abort_events: dict[UUID, asyncio.Event] = {}

    def request_abort(self, session_id: UUID) -> bool:
        event = self._abort_events.get(session_id)
        if event is not None:
            event.set()
            return True
        return False

    async def create(self, operator_id: UUID) -> ClaudeChatSessionView:
        view, token = await self._store.create(operator_id)
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
        if view.status != "provisioning":
            return view
        try:
            provisioning = await self._claims.inspect(session_id=view.session_id)
        except Exception as error:
            provisioning = _provisioning_view(
                f"claude-{view.session_id.hex}", step="claim_created", observation_error=str(error)
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

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == "terminal":
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1008, reason="runner session is already terminal")
            return
        if authentication == "rejected":
            await websocket.close(code=1008, reason="invalid or consumed runner credential")
            return
        await websocket.accept()
        adapter = StarletteTextWebSocket(websocket)
        options = enable_fine_grained_streaming(
            ClaudeAgentOptions(
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
        client = ClaudeSDKClient(options=options, transport=WebSocketTransport(adapter, build_claude_launch(options)))
        abort_event = asyncio.Event()
        self._abort_events[session_id] = abort_event
        try:
            await client.connect()
            while True:
                status = await self._store.status(session_id)
                if status in {None, "closing", "closed", "failed"}:
                    break
                prompt = await self._store.next_prompt(session_id)
                if prompt is None:
                    # Wait for a LISTEN/NOTIFY instead of polling.
                    await self._store.wait_for_prompt(session_id, timeout_seconds=30.0)
                    continue
                _, text = prompt
                try:
                    await self._run_turn(client, session_id, text, abort_event=abort_event)
                except Exception as error:
                    await self._store.fail(session_id, str(error))
                    break
                abort_event.clear()
        except WebSocketDisconnect:
            await self._store.fail(session_id, "sandbox runner disconnected")
        except Exception as error:
            await self._store.fail(session_id, f"Claude runtime failed: {error}")
        finally:
            self._abort_events.pop(session_id, None)
            await client.disconnect()
            await self._cleanup_terminal_claim(session_id)
            await self._store.closed(session_id)

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
        role=cast(MessageRole, message.role),
        status=cast(MessageStatus, message.status),
        content=message.content,
        tool_uses=[ClaudeChatToolUseView.model_validate(tool_use) for tool_use in message.tool_uses],
        error=message.error,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _session_view(record: ClaudeChatSession, messages: list[ClaudeChatMessage]) -> ClaudeChatSessionView:
    return ClaudeChatSessionView(
        session_id=record.session_id,
        status=cast(SessionStatus, record.status),
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


ClaudeChatServiceDep = Annotated[ClaudeChatService, Depends(_service)]
ClaudeChatStoreDep = Annotated[ClaudeChatStore, Depends(_store)]


@router.post("/api/claude/sessions")
async def create_session(actor: OperatorActorDep, service: ClaudeChatServiceDep) -> ClaudeChatSessionView:
    try:
        return await service.create(actor.operator_id)
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
    store: ClaudeChatStore, operator_id: UUID, session_id: UUID
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
        if last_view.status in {"closed", "failed"}:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        await store.listen_for_update(session_id, timeout_seconds=30.0)
        try:
            next_view = await store.get(operator_id, session_id)
        except KeyError:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        if next_view.model_dump_json() != last_view.model_dump_json():
            last_view = next_view
            yield f"data: {next_view.model_dump_json()}\n\n"


@router.get("/api/claude/sessions/{session_id}/stream")
async def stream_session(session_id: UUID, actor: OperatorActorDep, store: ClaudeChatStoreDep) -> StreamingResponse:
    if not await store.session_exists(actor.operator_id, session_id):
        raise HTTPException(status_code=404, detail="Claude chat session not found")
    return StreamingResponse(
        _sse_stream(store, actor.operator_id, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/claude/sessions/{session_id}/abort", status_code=202)
async def abort_session(session_id: UUID, actor: OperatorActorDep, service: ClaudeChatServiceDep) -> dict[str, str]:
    if not await service._store.session_exists(actor.operator_id, session_id):
        raise HTTPException(status_code=404, detail="Claude chat session not found")
    if not service.request_abort(session_id):
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
