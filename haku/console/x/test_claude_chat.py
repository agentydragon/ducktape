"""Focused contracts for the Agent Sandbox Claude chat runtime."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from kubernetes_asyncio import client as k8s_client
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ClaudeChatSession
from haku.console.x.claude_chat import (
    ClaudeChatService,
    ClaudeChatSessionView,
    ClaudeChatStore,
    KubernetesSandboxClaims,
    _provisioning_view,
    _text_delta,
)


class RecordingCustomObjectsApi:
    def __init__(self) -> None:
        self.created: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_namespaced_custom_object(self, *args: Any, **kwargs: Any) -> None:
        self.created = (args, kwargs)

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        del group, version, namespace
        return self.objects[(plural, name)]


class RecordingCoreV1Api:
    def __init__(self) -> None:
        self.pods: dict[str, k8s_client.V1Pod] = {}

    async def read_namespaced_pod(self, name: str, namespace: str) -> k8s_client.V1Pod:
        del namespace
        return self.pods[name]


class FailingEngine:
    def connect(self) -> Any:
        raise RuntimeError("LISTEN unavailable")


def _runtime_config(**overrides: object) -> ClaudeRuntimeConfig:
    values: dict[str, object] = {
        "namespace": "haku-claude-sandbox",
        "warm_pool": "haku-claude",
        "cwd": "/workspace",
        "session_ttl_seconds": 7200,
        "oauth_placeholder": "not-a-secret",
        "https_proxy": "http://proxy.test:8180",
        "ca_bundle": "/egress-proxy-ca/ca-certificates.crt",
        "no_proxy": "127.0.0.1,localhost,.svc,.svc.cluster.local,kubernetes.default.svc,10.0.0.0/8",
        "mcp_url": "http://haku-console.test:9090/mcp",
        "mcp_static_agent_id": "00000000-0000-4000-8000-000000000001",
    }
    values.update(overrides)
    return ClaudeRuntimeConfig.model_validate(values)


def test_runtime_deployment_wiring_has_no_application_defaults() -> None:
    assert all(field.is_required() for field in ClaudeRuntimeConfig.model_fields.values())


def _claims(
    config: ClaudeRuntimeConfig,
) -> tuple[KubernetesSandboxClaims, RecordingCustomObjectsApi, RecordingCoreV1Api]:
    claims = KubernetesSandboxClaims(config)
    custom = RecordingCustomObjectsApi()
    core = RecordingCoreV1Api()
    claims._custom_objects = cast(Any, custom)
    claims._core_v1 = cast(Any, core)
    return claims, custom, core


async def test_claim_injects_only_the_session_rendezvous_values() -> None:
    config = _runtime_config(oauth_placeholder="sk-ant-oat01-proxy-haku-claude-placeholder")
    claims, api, _ = _claims(config)
    session_id = UUID("10000000-0000-4000-8000-000000000001")

    await claims.create(
        session_id=session_id, bridge_token="one-use-secret", expires_at=datetime(2026, 8, 1, 5, 0, tzinfo=UTC)
    )

    assert api.created is not None
    args, _ = api.created
    assert args[:4] == ("extensions.agents.x-k8s.io", "v1beta1", "haku-claude-sandbox", "sandboxclaims")
    body = args[4]
    assert body["metadata"]["name"] == "claude-10000000000040008000000000000001"
    assert body["spec"]["warmPoolRef"] == {"name": "haku-claude"}
    assert body["spec"]["env"] == [
        {"name": "HAKU_CLAUDE_SESSION_ID", "value": str(session_id)},
        {"name": "HAKU_AGENT_SDK_RUNNER_TOKEN", "value": "one-use-secret"},
    ]
    assert body["spec"]["lifecycle"] == {"shutdownPolicy": "DeleteForeground", "shutdownTime": "2026-08-01T05:00:00Z"}


async def test_inspect_reports_each_underlying_provisioning_layer() -> None:
    config = _runtime_config()
    claims, custom, core = _claims(config)
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    claim_name = "claude-10000000000040008000000000000001"
    custom.objects[("sandboxclaims", claim_name)] = {
        "status": {
            "sandbox": {"name": "sandbox-abc"},
            "conditions": [{"type": "Ready", "status": "False", "reason": "PodNotReady", "message": "Waiting for Pod"}],
        }
    }
    custom.objects[("sandboxes", "sandbox-abc")] = {
        "metadata": {"annotations": {"agents.x-k8s.io/pod-name": "sandbox-pod-abc"}},
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }
    core.pods["sandbox-pod-abc"] = k8s_client.V1Pod(
        status=k8s_client.V1PodStatus(
            phase="Pending",
            conditions=[k8s_client.V1PodCondition(type="Ready", status="False")],
            container_statuses=[
                k8s_client.V1ContainerStatus(
                    name="runner",
                    image="runner:test",
                    image_id="",
                    ready=False,
                    restart_count=0,
                    state=k8s_client.V1ContainerState(
                        waiting=k8s_client.V1ContainerStateWaiting(reason="ContainerCreating")
                    ),
                )
            ],
        )
    )

    info = await claims.inspect(session_id=session_id)

    assert info.step == "waiting_for_pod_ready"
    assert info.claim_name == claim_name
    assert info.claim_ready is False
    assert info.claim_reason == "PodNotReady"
    assert info.claim_message == "Waiting for Pod"
    assert info.sandbox_name == "sandbox-abc"
    assert info.sandbox_ready is False
    assert info.pod_name == "sandbox-pod-abc"
    assert info.pod_phase == "Pending"
    assert info.pod_ready is False
    assert info.runner_ready is False
    assert info.runner_state == "waiting: ContainerCreating"


async def test_inspect_distinguishes_ready_pod_from_runner_bridge_wait() -> None:
    config = _runtime_config()
    claims, custom, core = _claims(config)
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    claim_name = "claude-10000000000040008000000000000001"
    custom.objects[("sandboxclaims", claim_name)] = {
        "status": {"sandbox": {"name": "sandbox-abc"}, "conditions": [{"type": "Ready", "status": "True"}]}
    }
    custom.objects[("sandboxes", "sandbox-abc")] = {
        "metadata": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    core.pods["sandbox-abc"] = k8s_client.V1Pod(
        status=k8s_client.V1PodStatus(
            phase="Running",
            conditions=[k8s_client.V1PodCondition(type="Ready", status="True")],
            container_statuses=[
                k8s_client.V1ContainerStatus(
                    name="runner",
                    image="runner:test",
                    image_id="runner:test",
                    ready=True,
                    restart_count=0,
                    state=k8s_client.V1ContainerState(running=k8s_client.V1ContainerStateRunning()),
                )
            ],
        )
    )

    info = await claims.inspect(session_id=session_id)

    assert info.step == "waiting_for_runner"
    assert info.claim_ready is True
    assert info.sandbox_ready is True
    assert info.pod_ready is True
    assert info.runner_ready is True
    assert info.runner_state == "running"


def test_claude_environment_contains_placeholder_proxy_and_ca_only() -> None:
    config = _runtime_config(ca_bundle="/ca/bundle.pem")

    assert config.claude_environment() == {
        "CLAUDE_CODE_OAUTH_TOKEN": "not-a-secret",
        "HTTP_PROXY": "http://proxy.test:8180",
        "HTTPS_PROXY": "http://proxy.test:8180",
        "NO_PROXY": "127.0.0.1,localhost,.svc,.svc.cluster.local,kubernetes.default.svc,10.0.0.0/8",
        "NODE_USE_ENV_PROXY": "1",
        "NODE_EXTRA_CA_CERTS": "/ca/bundle.pem",
        "SSL_CERT_FILE": "/ca/bundle.pem",
        "CURL_CA_BUNDLE": "/ca/bundle.pem",
        "REQUESTS_CA_BUNDLE": "/ca/bundle.pem",
    }


def test_text_delta_ignores_non_text_stream_events() -> None:
    assert _text_delta({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}) == "hi"
    assert _text_delta({"type": "content_block_delta", "delta": {"type": "input_json_delta"}}) == ""
    assert _text_delta({"type": "message_start"}) == ""


async def test_bridge_authentication_distinguishes_accept_terminal_and_rejected(
    migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("claude-chat-test")
    store = ClaudeChatStore(migrated_sessions, migrated_engine)
    view, token = await store.create(operator_id)
    session_id = view.session_id

    assert await store.authenticate_bridge(session_id, token) == "accepted"
    async with migrated_sessions() as db:
        record = await db.get(ClaudeChatSession, session_id)
        assert record is not None
        assert record.status == "ready"
        assert record.bridge_connected_at is not None
        # Retain only the hash until claim deletion completes. It lets terminal retries prove that
        # they belong to the stale claim without retaining or recovering the bearer itself.
        assert record.bridge_token_fingerprint == ClaudeChatStore._fingerprint(token)

    await store.fail(session_id, "runner failed")
    assert await store.authenticate_bridge(session_id, token) == "terminal"
    assert await store.authenticate_bridge(session_id, "wrong") == "rejected"


async def test_listen_failure_is_not_reclassified_as_a_timeout() -> None:
    store = ClaudeChatStore(cast(Any, object()), cast(Any, FailingEngine()))

    with pytest.raises(RuntimeError, match="LISTEN unavailable"):
        await store.wait_for_prompt(uuid4(), timeout_seconds=0.01)


async def test_deliberate_close_is_not_reclassified_as_runner_failure(
    migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("claude-chat-close-test")
    store = ClaudeChatStore(migrated_sessions, migrated_engine)
    view, _token = await store.create(operator_id)

    await store.request_close(operator_id, view.session_id)
    await store.fail(view.session_id, "sandbox runner disconnected")
    closing = await store.get(operator_id, view.session_id)
    assert closing.status == "closing"
    assert closing.error is None

    await store.complete_claim_cleanup(view.session_id)
    closed = await store.get(operator_id, view.session_id)
    assert closed.status == "closed"
    async with migrated_sessions() as db:
        record = await db.get(ClaudeChatSession, view.session_id)
        assert record is not None
        assert record.bridge_token_fingerprint == b""


class _LifecycleStore:
    def __init__(self, session_id: UUID, token: str):
        self.session_id = session_id
        self.token = token
        self.status_value = "provisioning"
        self.cleanup_completed: list[UUID] = []
        self.closed_sessions: list[UUID] = []

    async def create(self, operator_id: UUID) -> tuple[ClaudeChatSessionView, str]:
        del operator_id
        now = datetime.now(UTC)
        return (
            ClaudeChatSessionView(
                session_id=self.session_id,
                status="provisioning",
                error=None,
                created_at=now,
                updated_at=now,
                messages=[],
            ),
            self.token,
        )

    async def authenticate_bridge(self, session_id: UUID, token: str) -> str:
        assert session_id == self.session_id
        assert token == self.token
        self.status_value = "closing"
        return "accepted"

    async def status(self, session_id: UUID) -> str:
        assert session_id == self.session_id
        return self.status_value

    async def watch_aborts(self, session_id: UUID, on_abort: Callable[[], None]) -> None:
        del session_id, on_abort
        await asyncio.Event().wait()  # no aborts in this test; just never return

    async def complete_claim_cleanup(self, session_id: UUID) -> None:
        self.cleanup_completed.append(session_id)

    async def closed(self, session_id: UUID) -> None:
        self.closed_sessions.append(session_id)


class _LifecycleClaims:
    def __init__(self):
        self.created: list[UUID] = []
        self.deleted: list[UUID] = []

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        assert bridge_token == "bridge-token"
        assert expires_at > datetime.now(UTC)
        self.created.append(session_id)

    async def inspect(self, *, session_id: UUID):
        return _provisioning_view(f"claude-{session_id.hex}", step="claim_created")

    async def delete(self, *, session_id: UUID) -> None:
        self.deleted.append(session_id)

    async def aclose(self) -> None:
        return None


class _ToolUseStore:
    def __init__(self):
        self.message_ids: list[UUID] = []
        self.updates: list[tuple[UUID, str, list[dict[str, Any]] | None, bool]] = []
        self.completed_turns: list[UUID] = []

    async def begin_assistant(self, session_id: UUID) -> UUID:
        del session_id
        message_id = uuid4()
        self.message_ids.append(message_id)
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
        del session_id
        self.updates.append((message_id, content, tool_uses, complete))

    async def complete_turn(self, session_id: UUID) -> None:
        self.completed_turns.append(session_id)


class _ToolUseClaudeClient:
    async def query(self, prompt: str) -> None:
        assert prompt == "Check the Haku MCP catalog"

    async def receive_response(self):
        yield AssistantMessage(
            content=[ToolUseBlock(id="toolu_01", name="mcp__haku-console__haku-console__list_mcp_servers", input={})],
            model="claude-sonnet-5",
        )
        yield AssistantMessage(
            content=[TextBlock(text="The Haku Console catalog is available.")], model="claude-sonnet-5"
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=2,
            session_id="sdk-session",
            result="The Haku Console catalog is available.",
        )


async def test_run_turn_preserves_assistant_message_boundaries_around_tool_use() -> None:
    store = _ToolUseStore()
    service = ClaudeChatService(
        _runtime_config(), cast(Any, store), cast(Any, _LifecycleClaims()), mcp_token=SecretStr("unused")
    )

    session_id = uuid4()
    await service._run_turn(
        cast(Any, _ToolUseClaudeClient()), session_id, "Check the Haku MCP catalog", abort_event=asyncio.Event()
    )

    expected = [{"tool_use_id": "toolu_01", "name": "mcp__haku-console__haku-console__list_mcp_servers", "input": {}}]
    assert len(store.message_ids) == 2
    assert store.message_ids[0] != store.message_ids[1]
    assert store.updates == [
        (store.message_ids[0], "", expected, True),
        (store.message_ids[1], "The Haku Console catalog is available.", [], True),
    ]
    assert store.completed_turns == [session_id]


class _LifecycleWebSocket:
    def __init__(self):
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _LifecycleClaudeClient:
    last_options: object | None = None

    def __init__(self, **kwargs: object):
        type(self).last_options = kwargs["options"]
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True


async def test_session_lifecycle_creates_claim_accepts_bridge_and_disposes_claim() -> None:
    session_id = uuid4()
    store = _LifecycleStore(session_id, "bridge-token")
    claims = _LifecycleClaims()
    service = ClaudeChatService(
        _runtime_config(), cast(Any, store), cast(Any, claims), mcp_token=SecretStr("haku-static-bearer")
    )
    websocket = _LifecycleWebSocket()

    session = await service.create(uuid4())
    with patch("haku.console.x.claude_chat.ClaudeSDKClient", _LifecycleClaudeClient):
        await service.handle_runner(cast(Any, websocket), session_id, "bridge-token")

    assert session.session_id == session_id
    assert claims.created == [session_id]
    assert websocket.accepted is True
    assert websocket.closed is None
    assert claims.deleted == [session_id]
    assert store.cleanup_completed == [session_id]
    assert store.closed_sessions == [session_id]
    options = cast(Any, _LifecycleClaudeClient.last_options)
    assert options.mcp_servers == {
        "haku-console": {
            "type": "http",
            "url": "http://haku-console.test:9090/mcp",
            "headers": {"Authorization": "Bearer haku-static-bearer"},
        }
    }
    assert options.strict_mcp_config is True
    assert "haku-static-bearer" not in options.env.values()


class _TerminalStore:
    def __init__(self):
        self.completed: list[UUID] = []

    async def authenticate_bridge(self, session_id: UUID, token: str) -> str:
        del session_id, token
        return "terminal"

    async def complete_claim_cleanup(self, session_id: UUID) -> None:
        self.completed.append(session_id)


async def test_terminal_runner_retry_deletes_its_stale_claim() -> None:
    session_id = uuid4()
    store = _TerminalStore()
    claims = _LifecycleClaims()
    service = ClaudeChatService(
        _runtime_config(), cast(Any, store), cast(Any, claims), mcp_token=SecretStr("haku-static-bearer")
    )
    websocket = _LifecycleWebSocket()

    await service.handle_runner(cast(Any, websocket), session_id, "stale-but-authentic")

    assert claims.deleted == [session_id]
    assert store.completed == [session_id]
    assert websocket.closed == (1008, "runner session is already terminal")


class _ReconcileStore:
    def __init__(self, session_ids: list[UUID]):
        self.session_ids = session_ids
        self.completed: list[UUID] = []

    async def claim_cleanup_candidates(self) -> list[UUID]:
        return self.session_ids

    async def complete_claim_cleanup(self, session_id: UUID) -> None:
        self.completed.append(session_id)


async def test_startup_reconciliation_retries_terminal_claim_cleanup() -> None:
    session_ids = [uuid4(), uuid4()]
    store = _ReconcileStore(session_ids)
    claims = _LifecycleClaims()
    service = ClaudeChatService(
        _runtime_config(), cast(Any, store), cast(Any, claims), mcp_token=SecretStr("haku-static-bearer")
    )

    await service.reconcile_terminal_claims()

    assert claims.deleted == session_ids
    assert store.completed == session_ids


if __name__ == "__main__":
    pytest_bazel.main()


class _RealDbClaudeClient(_LifecycleClaudeClient):
    """Answers one prompt, then behaves like an idle client."""

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    def receive_response(self):
        async def _messages():
            yield AssistantMessage(content=[TextBlock(text="pong")], model="test")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                result="pong",
            )

        return _messages()


async def test_runner_survives_an_idle_wait_against_a_real_database(
    migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    """The idle wait is a raw-driver call, so only a real engine exercises it.

    `handle_runner` loops: consume a prompt, then block in `wait_for_prompt` until the next
    one. That wait talks to `driver_connection` directly, and the existing lifecycle test
    fakes the store, so a driver-API mismatch there was invisible — it shipped, and every
    Matrix session died about four seconds after being created with "Claude runtime failed:
    'Connection' object has no attribute 'set_autocommit'". Faking Kubernetes is right;
    faking the store hid the bug.
    """
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("claude-chat-listen-test")
    store = ClaudeChatStore(migrated_sessions, migrated_engine)
    service = ClaudeChatService(
        _runtime_config(), store, cast(Any, _LifecycleClaims()), mcp_token=SecretStr("haku-static-bearer")
    )
    # The store mints the real bridge token; no claim is created because handle_runner only
    # ever deletes one on the way out, and Kubernetes is not what this test is about.
    view, token = await store.create(operator_id)

    with patch("haku.console.x.claude_chat.ClaudeSDKClient", _RealDbClaudeClient):
        runner = asyncio.create_task(service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token))
        try:
            # Long enough to reach the idle wait, which is where the crash used to happen.
            await asyncio.sleep(2)
            assert await store.status(view.session_id) == "ready", "the runner failed while waiting for a prompt"

            # And the wait must actually wake on NOTIFY rather than only time out. A bounded
            # poll rather than an Event: the thing under test is the runner's own wake, so the
            # test must observe it from outside instead of being handed a signal by it.
            await store.enqueue_prompt(operator_id, view.session_id, "ping")
            for _ in range(75):
                if await store.status(view.session_id) == "ready":
                    break
                await asyncio.sleep(0.2)
            assert await store.status(view.session_id) == "ready", "the turn never completed"
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    [answer] = [m for m in (await store.get(operator_id, view.session_id)).messages if m.role == "assistant"]
    assert answer.content == "pong"


async def test_abort_is_refused_when_no_turn_is_in_flight(
    migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    """An idle session has nothing to interrupt, and saying so is the point of the 409.

    The old check asked "is this session's abort event registered in *this* process", which
    is true for the whole life of the runner bridge — so aborting an idle session set the
    event, and the next turn aborted the instant it started.
    """
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("claude-chat-abort-idle")
    store = ClaudeChatStore(migrated_sessions, migrated_engine)
    view, token = await store.create(operator_id)
    # The bridge handshake is what takes a session from provisioning to ready, and only a
    # ready session accepts a prompt.
    assert await store.authenticate_bridge(view.session_id, token) == "accepted"

    assert await store.request_abort(view.session_id) is False

    await store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await store.request_abort(view.session_id) is True


async def test_abort_reaches_the_replica_running_the_turn(
    migrated_db_url, migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    """The two ends of an abort are on different pods, so it has to cross the database.

    The abort event belongs to whichever replica holds the runner's bridge websocket, while
    `POST .../abort` is balanced across all of them — at `replicas: 2` the operator's abort
    button therefore failed with a spurious 409 about half the time. Two stores over two
    engines is what reproduces that; a single store would pass on the in-process path this
    change removes.
    """
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("claude-chat-abort-cross")
    running = ClaudeChatStore(migrated_sessions, migrated_engine)
    view, token = await running.create(operator_id)
    assert await running.authenticate_bridge(view.session_id, token) == "accepted"
    await running.enqueue_prompt(operator_id, view.session_id, "work")

    other_engine = create_async_engine(migrated_db_url, pool_pre_ping=True)
    try:
        requesting = ClaudeChatStore(async_sessionmaker(other_engine, expire_on_commit=False), other_engine)
        aborted = asyncio.Event()
        watcher = asyncio.create_task(running.watch_aborts(view.session_id, aborted.set))
        try:
            # Retry rather than sleep a magic interval: the LISTEN registers asynchronously,
            # and re-notifying is harmless, so this waits for readiness without guessing it.
            async with asyncio.timeout(30):
                while not aborted.is_set():
                    assert await requesting.request_abort(view.session_id) is True
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(0.5):
                            await aborted.wait()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
    finally:
        await other_engine.dispose()
