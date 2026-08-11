"""Focused contracts for the Agent Sandbox Claude chat runtime."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_bazel
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from haku.console.chat_models import ChatMessageRole, ChatMessageStatus, ChatSessionStatus
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ClaudeChatSession
from haku.console.x.chat_notifications import ChatEventKind
from haku.console.x.claude_chat import BridgeAuthentication, ClaudeChatStore, KubernetesSandboxClaims, _text_delta
from haku.console.x.conftest import runtime_config


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


def test_runtime_deployment_wiring_has_no_application_defaults() -> None:
    assert all(field.is_required() for field in ClaudeRuntimeConfig.model_fields.values())


@pytest.fixture
def custom_objects_api() -> RecordingCustomObjectsApi:
    return RecordingCustomObjectsApi()


@pytest.fixture
def core_v1_api() -> RecordingCoreV1Api:
    return RecordingCoreV1Api()


@pytest.fixture
def sandbox_claims(custom_objects_api, core_v1_api) -> KubernetesSandboxClaims:
    """The real claim builder with only the Kubernetes API objects recorded."""
    claims = KubernetesSandboxClaims(runtime_config())
    claims._custom_objects = cast(Any, custom_objects_api)
    claims._core_v1 = cast(Any, core_v1_api)
    return claims


async def test_claim_injects_only_the_session_rendezvous_values(sandbox_claims, custom_objects_api) -> None:
    session_id = UUID("10000000-0000-4000-8000-000000000001")

    await sandbox_claims.create(
        session_id=session_id, bridge_token="one-use-secret", expires_at=datetime(2026, 8, 1, 5, 0, tzinfo=UTC)
    )

    assert custom_objects_api.created is not None
    args, _ = custom_objects_api.created
    assert args[:4] == ("extensions.agents.x-k8s.io", "v1beta1", "haku-claude-sandbox", "sandboxclaims")
    body = args[4]
    assert body["metadata"]["name"] == "claude-10000000000040008000000000000001"
    assert body["spec"]["warmPoolRef"] == {"name": "haku-claude"}
    assert body["spec"]["env"] == [
        {"name": "HAKU_CLAUDE_SESSION_ID", "value": str(session_id)},
        {"name": "HAKU_AGENT_SDK_RUNNER_TOKEN", "value": "one-use-secret"},
    ]
    assert body["spec"]["lifecycle"] == {"shutdownPolicy": "DeleteForeground", "shutdownTime": "2026-08-01T05:00:00Z"}


async def test_inspect_reports_each_underlying_provisioning_layer(
    sandbox_claims, custom_objects_api, core_v1_api
) -> None:
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    claim_name = "claude-10000000000040008000000000000001"
    custom_objects_api.objects[("sandboxclaims", claim_name)] = {
        "status": {
            "sandbox": {"name": "sandbox-abc"},
            "conditions": [{"type": "Ready", "status": "False", "reason": "PodNotReady", "message": "Waiting for Pod"}],
        }
    }
    custom_objects_api.objects[("sandboxes", "sandbox-abc")] = {
        "metadata": {"annotations": {"agents.x-k8s.io/pod-name": "sandbox-pod-abc"}},
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }
    core_v1_api.pods["sandbox-pod-abc"] = k8s_client.V1Pod(
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

    info = await sandbox_claims.inspect(session_id=session_id)

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


async def test_inspect_distinguishes_ready_pod_from_runner_bridge_wait(
    sandbox_claims, custom_objects_api, core_v1_api
) -> None:
    session_id = UUID("10000000-0000-4000-8000-000000000001")
    claim_name = "claude-10000000000040008000000000000001"
    custom_objects_api.objects[("sandboxclaims", claim_name)] = {
        "status": {"sandbox": {"name": "sandbox-abc"}, "conditions": [{"type": "Ready", "status": "True"}]}
    }
    custom_objects_api.objects[("sandboxes", "sandbox-abc")] = {
        "metadata": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    core_v1_api.pods["sandbox-abc"] = k8s_client.V1Pod(
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

    info = await sandbox_claims.inspect(session_id=session_id)

    assert info.step == "waiting_for_runner"
    assert info.claim_ready is True
    assert info.sandbox_ready is True
    assert info.pod_ready is True
    assert info.runner_ready is True
    assert info.runner_state == "running"


def test_claude_environment_contains_placeholder_proxy_and_ca_only() -> None:
    config = runtime_config(ca_bundle="/ca/bundle.pem")

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
    chat_store, operator_id, migrated_sessions
) -> None:
    view, token = await chat_store.create(operator_id)
    session_id = view.session_id

    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    async with migrated_sessions() as db:
        record = await db.get(ClaudeChatSession, session_id)
        assert record is not None
        assert record.status == ChatSessionStatus.READY
        assert record.bridge_connected_at is not None
        # Retain only the hash until claim deletion completes. It lets terminal retries prove that
        # they belong to the stale claim without retaining or recovering the bearer itself.
        assert record.bridge_token_fingerprint == ClaudeChatStore._fingerprint(token)

    await chat_store.fail(session_id, "runner failed")
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.TERMINAL
    assert await chat_store.authenticate_bridge(session_id, "wrong") == BridgeAuthentication.REJECTED


async def test_deliberate_close_is_not_reclassified_as_runner_failure(
    chat_store, operator_id, migrated_sessions
) -> None:
    view, _token = await chat_store.create(operator_id)

    await chat_store.request_close(operator_id, view.session_id)
    await chat_store.fail(view.session_id, "sandbox runner disconnected")
    closing = await chat_store.get(operator_id, view.session_id)
    assert closing.status == ChatSessionStatus.CLOSING
    assert closing.error is None

    await chat_store.complete_claim_cleanup(view.session_id)
    closed = await chat_store.get(operator_id, view.session_id)
    assert closed.status == ChatSessionStatus.CLOSED
    async with migrated_sessions() as db:
        record = await db.get(ClaudeChatSession, view.session_id)
        assert record is not None
        assert record.bridge_token_fingerprint == b""


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


async def test_run_turn_preserves_assistant_message_boundaries_around_tool_use(
    chat_store, chat_service, operator_id
) -> None:
    """A tool-use block and the text after it are two messages, not one merged row."""
    view, token = await chat_store.create(operator_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    await chat_service._run_turn(
        cast(Any, _ToolUseClaudeClient()), view.session_id, "Check the Haku MCP catalog", abort_event=asyncio.Event()
    )

    messages = [
        m for m in (await chat_store.get(operator_id, view.session_id)).messages if m.role == ChatMessageRole.ASSISTANT
    ]
    assert [(m.content, [u.model_dump() for u in m.tool_uses], m.status) for m in messages] == [
        (
            "",
            [{"tool_use_id": "toolu_01", "name": "mcp__haku-console__haku-console__list_mcp_servers", "input": {}}],
            ChatMessageStatus.COMPLETE,
        ),
        ("The Haku Console catalog is available.", [], ChatMessageStatus.COMPLETE),
    ]
    assert await chat_store.status(view.session_id) == ChatSessionStatus.READY, "the turn was not completed"


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


class _ClosingClaudeClient(_LifecycleClaudeClient):
    """Closes the session on connect, so the runner's loop exits at its first status check.

    Something has to end the loop, which otherwise sits in a 30s `wait_for_prompt`. The fake
    store this replaced got there by lying in `authenticate_bridge`; putting it in the SDK
    client keeps the store real and the loop's own exit condition under test.
    """

    on_connect: Callable[[], Awaitable[None]] | None = None

    async def connect(self) -> None:
        await super().connect()
        on_connect = type(self).on_connect
        assert on_connect is not None
        await on_connect()


async def test_session_lifecycle_creates_claim_accepts_bridge_and_disposes_claim(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id)
    session_id = session.session_id
    _ClosingClaudeClient.on_connect = lambda: chat_store.request_close(operator_id, session_id)
    with patch("haku.console.x.claude_chat.ClaudeSDKClient", _ClosingClaudeClient):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert recording_claims.created == [session_id]
    assert websocket.accepted is True
    assert websocket.closed is None
    assert recording_claims.deleted == [session_id]
    assert await chat_store.status(session_id) == ChatSessionStatus.CLOSED
    # Cleanup is recorded by clearing the hashed rendezvous credential, which is what takes the
    # session back out of the reconciler's candidate set.
    assert await chat_store.claim_cleanup_candidates() == []
    options = cast(Any, _ClosingClaudeClient.last_options)
    assert options.mcp_servers == {
        "haku-console": {
            "type": "http",
            "url": "http://haku-console.test:9090/mcp",
            "headers": {"Authorization": "Bearer haku-static-bearer"},
        }
    }
    assert options.strict_mcp_config is True
    assert "haku-static-bearer" not in options.env.values()


async def test_terminal_runner_retry_deletes_its_stale_claim(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner presenting a valid credential for an already-closed session is turned away."""
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id)
    await chat_store.request_close(operator_id, session.session_id)

    await chat_service.handle_runner(
        cast(Any, websocket), session.session_id, recording_claims.tokens[session.session_id]
    )

    assert recording_claims.deleted == [session.session_id]
    assert await chat_store.claim_cleanup_candidates() == []
    assert await chat_store.status(session.session_id) == ChatSessionStatus.CLOSED
    assert websocket.closed == (1008, "runner session is already terminal")


async def test_startup_reconciliation_retries_terminal_claim_cleanup(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """Claims left behind by a Console that died mid-teardown are swept on the next boot."""

    session_ids = []
    for _ in range(2):
        session = await chat_service.create(operator_id)
        await chat_store.request_close(operator_id, session.session_id)
        session_ids.append(session.session_id)

    await chat_service.reconcile_terminal_claims()

    assert sorted(recording_claims.deleted) == sorted(session_ids)
    assert await chat_store.claim_cleanup_candidates() == []


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


async def test_runner_survives_an_idle_wait_against_a_real_database(chat_store, chat_service, operator_id) -> None:
    """The idle wait is a raw-driver call, so only a real engine exercises it.

    `handle_runner` loops: consume a prompt, then block in `wait_for_prompt` until the next
    one. That wait talks to `driver_connection` directly, and the existing lifecycle test
    fakes the store, so a driver-API mismatch there was invisible — it shipped, and every
    Matrix session died about four seconds after being created with "Claude runtime failed:
    'Connection' object has no attribute 'set_autocommit'". Faking Kubernetes is right;
    faking the store hid the bug.
    """
    # The store mints the real bridge token; no claim is created because handle_runner only
    # ever deletes one on the way out, and Kubernetes is not what this test is about.
    view, token = await chat_store.create(operator_id)

    with patch("haku.console.x.claude_chat.ClaudeSDKClient", _RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        try:
            # Long enough to reach the idle wait, which is where the crash used to happen.
            await asyncio.sleep(2)
            assert await chat_store.status(view.session_id) == ChatSessionStatus.READY, (
                "the runner failed while waiting for a prompt"
            )

            # And the wait must actually wake on NOTIFY rather than only time out. A bounded
            # poll rather than an Event: the thing under test is the runner's own wake, so the
            # test must observe it from outside instead of being handed a signal by it.
            await chat_store.enqueue_prompt(operator_id, view.session_id, "ping")
            for _ in range(75):
                if await chat_store.status(view.session_id) == ChatSessionStatus.READY:
                    break
                await asyncio.sleep(0.2)
            assert await chat_store.status(view.session_id) == ChatSessionStatus.READY, "the turn never completed"
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    [answer] = [
        m for m in (await chat_store.get(operator_id, view.session_id)).messages if m.role == ChatMessageRole.ASSISTANT
    ]
    assert answer.content == "pong"


async def test_abort_is_refused_when_no_turn_is_in_flight(chat_store, operator_id) -> None:
    """An idle session has nothing to interrupt, and saying so is the point of the 409.

    The old check asked "is this session's abort event registered in *this* process", which
    is true for the whole life of the runner bridge — so aborting an idle session set the
    event, and the next turn aborted the instant it started.
    """
    view, token = await chat_store.create(operator_id)
    # The bridge handshake is what takes a session from provisioning to ready, and only a
    # ready session accepts a prompt.
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await chat_store.request_abort(view.session_id) is False

    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.request_abort(view.session_id) is True


async def test_abort_reaches_the_replica_running_the_turn(
    migrated_db_url, chat_store, notifications, operator_id
) -> None:
    """The two ends of an abort are on different pods, so it has to cross the database.

    The abort event belongs to whichever replica holds the runner's bridge websocket, while
    `POST .../abort` is balanced across all of them — at `replicas: 2` the operator's abort
    button therefore failed with a spurious 409 about half the time. Two stores over two
    engines is what reproduces that; a single store would pass on the in-process path this
    change removes.
    """
    view, token = await chat_store.create(operator_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")

    other_engine = create_async_engine(migrated_db_url, pool_pre_ping=True)
    try:
        requesting = ClaudeChatStore(async_sessionmaker(other_engine, expire_on_commit=False))
        async with notifications.subscribe(ChatEventKind.ABORT, view.session_id) as aborted:
            assert await requesting.request_abort(view.session_id) is True
            async with asyncio.timeout(30):
                await aborted.wait()
    finally:
        await other_engine.dispose()


async def _age_lease(sessions: async_sessionmaker[AsyncSession], session_id: UUID, *, seconds_ago: int) -> None:
    async with sessions.begin() as db:
        chat = await db.get(ClaudeChatSession, session_id)
        assert chat is not None
        chat.lease_expires_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)


async def test_a_live_session_whose_holder_stopped_renewing_is_failed(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The wedge this exists for: a live status nobody is working on.

    A replica that dies without running its finalizer leaves `responding` behind, and every
    other observer used to treat that as healthy — so the room was never answered and never
    told why. The expired lease is the evidence that makes it reclaimable by anyone.
    """
    view, _ = await chat_store.create(operator_id)
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 1
    assert await chat_store.status(view.session_id) == ChatSessionStatus.FAILED
    assert "went away" in (await chat_store.get(operator_id, view.session_id)).error


async def test_a_session_whose_holder_is_still_renewing_is_left_alone(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A busy replica must not have its session reclaimed out from under it."""
    view, _ = await chat_store.create(operator_id)
    await chat_store.renew_lease(view.session_id)

    assert await chat_store.expire_stale_leases() == 0
    assert await chat_store.status(view.session_id) == ChatSessionStatus.PROVISIONING


async def test_an_ended_session_is_not_reclassified_by_the_sweep(chat_store, migrated_sessions, operator_id) -> None:
    """Only a *live* status is a lie worth correcting; a terminal one is already the truth."""
    view, _ = await chat_store.create(operator_id)
    await chat_store.fail(view.session_id, "something else went wrong first")
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 0
    assert (await chat_store.get(operator_id, view.session_id)).error == "something else went wrong first"


async def test_a_cancelled_runner_records_the_shutdown_instead_of_going_quiet(
    chat_store, chat_service, operator_id
) -> None:
    """Pod termination cancels this task, and `CancelledError` is not an `Exception`.

    So neither `except` clause saw it: the session kept its live status, the replica went
    away, and the room waited forever. The status must end terminal, and say so.
    """
    view, token = await chat_store.create(operator_id)

    with patch("haku.console.x.claude_chat.ClaudeSDKClient", _RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        await asyncio.sleep(2)  # Long enough to reach the idle wait, as the sibling test does.
        assert await chat_store.status(view.session_id) == ChatSessionStatus.READY

        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert await chat_store.status(view.session_id) == ChatSessionStatus.FAILED
    assert "shut down" in (await chat_store.get(operator_id, view.session_id)).error
