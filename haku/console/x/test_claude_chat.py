"""Focused contracts for the Agent Sandbox Claude chat runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from haku.console.chat_models import (
    ChatMessageRole,
    ChatMessageStatus,
    ChatSessionStatus,
    ChatSurface,
    FrameDirection,
    TurnOutcome,
)
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ClaudeChatFrame, ClaudeChatMessage, ClaudeChatPrompt, ClaudeChatSession
from haku.console.x.chat_notifications import ChatEventKind, ChatNotifications
from haku.console.x.claude_chat import (
    ABORTED_NOTICE,
    GOING_AWAY_CODE,
    REPLICA,
    STATUS_AFTER_SECONDS,
    TYPING_REFRESH_SECONDS,
    BridgeAuthentication,
    ClaudeChatService,
    ClaudeChatStore,
    KubernetesSandboxClaims,
    MatrixSession,
    RolloutRecorder,
    SpaSession,
    _coarse_status,
    _text_delta,
    _TurnStatus,
)
from haku.console.x.conftest import (
    MATRIX_CONFIG,
    MATRIX_OPERATOR,
    MATRIX_ROOM,
    MATRIX_USER,
    MCP_TOKEN,
    RecordingClaims,
    runtime_config,
)
from haku.console.x.matrix_client import InboundMessage
from haku.console.x.matrix_session import MatrixTurns
from haku.runtime.x.claude_bridge.cli_client import ClaudeCli


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
    view, token = await chat_store.create(operator_id, SpaSession())
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
    view, _token = await chat_store.create(operator_id, SpaSession())

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


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _result(text: str = "", **fields: Any) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, **fields}


class _FakeCli:
    """A `ClaudeCli` that replays scripted frames.

    Frames rather than SDK objects, because that is what the runtime now consumes — so a test
    double cannot drift from the wire by being easier to construct than the wire is.
    """

    def __init__(self, script: list[dict[str, Any]] | None = None):
        self.script = list(script or [])
        self.prompts: list[str] = []
        self.interrupted = False
        self.closed = False
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self) -> dict[str, Any]:
        return {"subtype": "success"}

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        for frame in self.script:
            self._queue.put_nowait(frame)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def frames(self):
        # Never ends on its own: a real CLI stays open between turns, and a generator that
        # stopped after the first `result` would make the second turn look like a dead stream.
        while True:
            yield await self._queue.get()

    async def aclose(self) -> None:
        self.closed = True


_TOOL_USE_SCRIPT = [
    _assistant(
        {"type": "tool_use", "id": "toolu_01", "name": "mcp__haku-console__haku-console__list_mcp_servers", "input": {}}
    ),
    _assistant({"type": "text", "text": "The Haku Console catalog is available."}),
    _result("The Haku Console catalog is available."),
]


async def test_run_turn_preserves_assistant_message_boundaries_around_tool_use(
    chat_store, chat_service, operator_id
) -> None:
    """A tool-use block and the text after it are two messages, not one merged row."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "Check the Haku MCP catalog")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None

    client = _FakeCli(_TOOL_USE_SCRIPT)
    await chat_service._run_turn(
        cast(Any, client),
        client.frames().__aiter__(),
        view.session_id,
        turn.prompt,
        turn_id=turn.turn_id,
        room_id=None,
        abort_event=asyncio.Event(),
    )

    messages = [
        m for m in (await chat_store.get(operator_id, view.session_id)).messages if m.role == ChatMessageRole.ASSISTANT
    ]
    assert [(m.content, [u.model_dump() for u in m.tool_uses], m.status) for m in messages] == [
        (
            "",
            [
                {
                    "tool_use_id": "toolu_01",
                    "name": "mcp__haku-console__haku-console__list_mcp_servers",
                    "input": {},
                    # No `user` frame answered it in this test, and the view says so rather than
                    # showing an empty result.
                    "result": None,
                }
            ],
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


class _LifecycleClaudeClient(_FakeCli):
    last_launch: object | None = None

    def __init__(self, adapter: object, launch: object, on_progress: object = None, frames_to: object = None):
        super().__init__()
        type(self).last_launch = launch
        self.connected = False

    async def connect(self) -> dict[str, Any]:
        self.connected = True
        return {"subtype": "success"}


class _ClosingClaudeClient(_LifecycleClaudeClient):
    """Closes the session on connect, so the runner's loop exits at its first status check.

    Something has to end the loop, which otherwise sits in a 30s `wait_for_prompt`. The fake
    store this replaced got there by lying in `authenticate_bridge`; putting it in the SDK
    client keeps the store real and the loop's own exit condition under test.
    """

    on_connect: Callable[[], Awaitable[None]] | None = None

    async def connect(self) -> dict[str, Any]:
        response = await super().connect()
        on_connect = type(self).on_connect
        assert on_connect is not None
        await on_connect()
        return response


async def test_session_lifecycle_creates_claim_accepts_bridge_and_disposes_claim(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    _ClosingClaudeClient.on_connect = lambda: chat_store.request_close(operator_id, session_id)
    with patch("haku.console.x.claude_chat.cli_over_websocket", _ClosingClaudeClient):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert recording_claims.created == [session_id]
    assert websocket.accepted is True
    assert websocket.closed is None
    assert recording_claims.deleted == [session_id]
    assert await chat_store.status(session_id) == ChatSessionStatus.CLOSED
    # Cleanup is recorded by clearing the hashed rendezvous credential, which is what takes the
    # session back out of the reconciler's candidate set.
    assert await chat_store.claim_cleanup_candidates() == []
    # Asserted on the launch the runner is handed rather than on SDK options, since that is
    # what now crosses the wire — and it is where a bearer would leak if one ever did.
    launch = cast(Any, _ClosingClaudeClient.last_launch)
    assert json.loads(launch.arguments[launch.arguments.index("--mcp-config") + 1]) == {
        "mcpServers": {
            "haku-console": {
                "type": "http",
                "url": "http://haku-console.test:9090/mcp",
                "headers": {"Authorization": "Bearer haku-static-bearer"},
            }
        }
    }
    assert "--strict-mcp-config" in launch.arguments
    assert "haku-static-bearer" not in launch.environment.values()


class _NarratingClaudeClient(_LifecycleClaudeClient):
    """Says one bootstrap line through the progress sink, then ends the session."""

    on_connect: Callable[[], Awaitable[None]] | None = None

    def __init__(self, adapter: object, launch: object, on_progress: object = None, frames_to: object = None):
        super().__init__(adapter, launch, on_progress, frames_to)
        self._on_progress = cast(Callable[[str], Awaitable[None]], on_progress)

    async def connect(self) -> dict[str, Any]:
        response = await super().connect()
        await self._on_progress("Cloning into '/workspace/haku-state'...")
        on_connect = type(self).on_connect
        assert on_connect is not None
        await on_connect()
        return response


async def test_the_sandbox_narration_outlives_the_pod_that_wrote_it(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """Bootstrap output and the CLI's stderr are where a session that never reached the model
    explains itself, and both used to live only in the pod's log and in the room — the first
    reaped with the sandbox, the second interleaved with everything else.
    """
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    _NarratingClaudeClient.on_connect = lambda: chat_store.request_close(operator_id, session_id)
    with patch("haku.console.x.claude_chat.cli_over_websocket", _NarratingClaudeClient):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    frames = await chat_store.read_frames(str(session_id), after_seq=None, limit=10, kinds=["setup_output"])
    assert [frame.payload["text"] for frame in frames] == ["Cloning into '/workspace/haku-state'..."]


class _RollingClaudeClient(_LifecycleClaudeClient):
    """Stands in for this replica being cancelled mid-session, which is what a roll is."""

    async def connect(self) -> dict[str, Any]:
        await super().connect()
        raise asyncio.CancelledError


async def test_a_rolling_replica_hands_the_session_back_instead_of_ending_it(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The measured cause of sessions that record a boot and a death.

    A roll cancels `handle_runner`, which recorded `console replica shut down mid-session` and
    failed the row — so the runner's reconnect was refused as terminal and the whole session was
    replaced. Six rolls a day made that the ordinary end of a conversation.
    """
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    with (
        patch("haku.console.x.claude_chat.cli_over_websocket", _RollingClaudeClient),
        pytest.raises(asyncio.CancelledError),
    ):
        await chat_service.handle_runner(cast(Any, websocket), session_id, recording_claims.tokens[session_id])

    assert await chat_store.status(session_id) == ChatSessionStatus.READY, "a roll is not a session ending"
    assert recording_claims.deleted == [], "the sandbox outlives the replica that was serving it"
    assert websocket.closed == (GOING_AWAY_CODE, "console replica going away"), (
        "the runner reconnects because it was told to, not because it guessed"
    )


async def test_a_returning_runner_is_admitted_and_takes_the_lease(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A reconnect used to be refused unconditionally, which is what made the sandbox disposable."""
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    token = recording_claims.tokens[session_id]
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED

    with patch("haku.console.x.claude_chat.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.REJECTED, (
            "a replica still renewing its lease keeps the session it is serving"
        )
        await chat_store.release_lease(session_id)
        assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED, (
            "a session handed back is adoptable by whichever replica the runner reaches"
        )


async def test_adoption_closes_the_turn_the_previous_holder_left_open(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """The frames answering that prompt went to a socket that no longer exists — and one open
    turn per session is a schema property, so leaving it would wedge the session outright.
    """
    session = await chat_service.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
    await chat_store.enqueue_prompt(operator_id, session_id, "what were we doing")
    started = await chat_store.next_prompt(session_id)
    assert started is not None

    assert await chat_store.abandon_open_turn(session_id) == started.turn_id
    [turn] = await chat_store.list_turns(str(session_id), limit=5)
    assert turn.outcome == TurnOutcome.FAILED
    assert await chat_store.abandon_open_turn(session_id) is None, "nothing left open to abandon"


async def test_terminal_runner_retry_deletes_its_stale_claim(
    chat_store, chat_service, recording_claims, operator_id
) -> None:
    """A runner presenting a valid credential for an already-closed session is turned away."""
    websocket = _LifecycleWebSocket()

    session = await chat_service.create(operator_id, SpaSession())
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
        session = await chat_service.create(operator_id, SpaSession())
        await chat_store.request_close(operator_id, session.session_id)
        session_ids.append(session.session_id)

    await chat_service.reconcile_terminal_claims()

    assert sorted(recording_claims.deleted) == sorted(session_ids)
    assert await chat_store.claim_cleanup_candidates() == []


def test_a_tool_call_becomes_a_status_naming_the_tool_verbatim() -> None:
    """R6.3: the CLI's own identifier, with no per-tool copy to maintain."""
    frame = _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}})

    assert _coarse_status(frame) == "running Bash"


def test_a_task_frame_reuses_the_description_the_cli_already_wrote() -> None:
    frame = {"type": "system", "subtype": "task_progress", "description": "Running the test suite"}

    assert _coarse_status(frame) == "Running the test suite"


def test_frames_the_room_has_no_use_for_produce_no_status() -> None:
    assert _coarse_status({"type": "result", "subtype": "success"}) is None
    assert _coarse_status({"type": "system", "subtype": "commands_changed"}) is None


async def test_a_short_turn_leaves_no_status_behind() -> None:
    """R6.2: below the threshold the answer is the status, and a pair of them is clutter."""
    shown: list[str] = []
    status = _TurnStatus(_appender(shown), _noop)
    status.start()
    status.note(_assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}))
    await asyncio.sleep(1.2)
    await status.finish()

    assert shown == []


async def test_a_slow_turn_says_what_it_is_doing_and_then_retires_the_line() -> None:
    shown: list[str] = []
    cleared: list[bool] = []

    async def clear() -> None:
        cleared.append(True)

    status = _TurnStatus(_appender(shown), clear)
    status._started -= STATUS_AFTER_SECONDS  # the turn has already been running a while
    status.start()
    status.note(_assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}))
    await asyncio.sleep(1.2)
    await status.finish()

    assert shown == ["running Bash"]
    assert cleared == [True]


async def test_the_line_is_retired_even_when_the_turn_fails() -> None:
    """A line still saying \"running Bash\" after the turn died is the stuck-indicator bug."""
    cleared: list[bool] = []

    async def clear() -> None:
        cleared.append(True)

    status = _TurnStatus(_appender([]), clear)
    status.start()
    await status.finish()

    assert cleared == [True]


async def test_typing_starts_with_the_turn_rather_than_waiting_for_the_status_threshold() -> None:
    """R6.1: "Haku is working on it" is worth nothing after the fact, so unlike the status line
    it does not wait — a turn shorter than `STATUS_AFTER_SECONDS` still shows it."""
    typed: list[bool] = []

    status = _TurnStatus(_appender([]), _noop, _recorder(typed))
    status.start()
    await asyncio.sleep(1.2)
    await status.finish()

    assert typed == [True, False], "on at the start, off at the end, and no status line in between"


async def test_typing_is_refreshed_for_the_length_of_the_turn() -> None:
    """The homeserver expires the notice on its own — which is what keeps a dead console from
    leaving one stuck on — so a live turn has to keep saying it."""
    typed: list[bool] = []

    status = _TurnStatus(_appender([]), _noop, _recorder(typed))
    status._typed_at -= TYPING_REFRESH_SECONDS  # the last notice is already due for renewal
    status.start()
    await asyncio.sleep(1.2)

    assert typed == [True]
    status._typed_at -= TYPING_REFRESH_SECONDS
    await asyncio.sleep(1.2)
    await status.finish()

    assert typed == [True, True, False]


async def test_typing_is_taken_back_even_when_the_turn_fails() -> None:
    """The stuck typing indicator this requirement is named after: every terminal path clears it,
    failure included, and `finish()` is the one hook all of them run."""
    typed: list[bool] = []

    status = _TurnStatus(_appender([]), _noop, _recorder(typed))
    status.start()
    await status.finish()

    assert typed[-1] is False


def _recorder(sink: list[bool]) -> Callable[[bool], Awaitable[None]]:
    async def typing(active: bool) -> None:
        sink.append(active)

    return typing


def _appender(sink: list[str]) -> Callable[[str], Awaitable[None]]:
    async def show(text: str) -> None:
        sink.append(text)

    return show


async def _noop() -> None:
    pass


async def test_the_rollout_reads_back_in_wire_order_with_a_keyset_cursor(chat_store, operator_id) -> None:
    """Keyset, not offset: the log is append-only, so new frames landing between pages would
    make an offset skip or repeat a row."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    for kind in ("user", "assistant", "result"):
        await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, kind, {"type": kind})

    first = await chat_store.read_frames(str(session.session_id), after_seq=None, limit=2, kinds=None)
    rest = await chat_store.read_frames(str(session.session_id), after_seq=first[-1].frame_seq, limit=2, kinds=None)

    assert [frame.kind for frame in first] == ["user", "assistant"]
    assert [frame.kind for frame in rest] == ["result"]


async def test_the_kinds_filter_skims_without_paging_through_everything(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())
    for kind in ("user", "system", "assistant", "system", "result"):
        await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, kind, {"type": kind})

    frames = await chat_store.read_frames(
        str(session.session_id), after_seq=None, limit=25, kinds=["assistant", "result"]
    )

    assert [frame.kind for frame in frames] == ["assistant", "result"]


async def test_one_session_never_reads_another_session_frames(chat_store, operator_id) -> None:
    mine, _ = await chat_store.create(operator_id, SpaSession())
    theirs, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(mine.session_id, FrameDirection.FROM_AGENT, "assistant", {"type": "assistant"})
    await chat_store.record_frame(theirs.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"})

    frames = await chat_store.read_frames(str(mine.session_id), after_seq=None, limit=25, kinds=None)

    assert [frame.kind for frame in frames] == ["assistant"]


async def test_conversations_come_back_newest_first_with_the_room_they_served(chat_store, operator_id) -> None:
    await chat_store.create(operator_id, SpaSession())
    matrix, _ = await chat_store.create(operator_id, MatrixSession(room_id="!room:example.org"))

    conversations = await chat_store.list_conversations(limit=10)

    assert conversations[0].session_id == str(matrix.session_id)
    assert conversations[0].room_id == "!room:example.org"
    assert conversations[1].room_id is None


ROOM = "!room:example.org"

_NARRATED_TURN = [
    _assistant({"type": "text", "text": "Looking at the logs now."}),
    _assistant({"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "true"}}),
    _assistant({"type": "text", "text": "Found it: a bad config."}),
    _result("Found it: a bad config."),
]


class _RecordingRoomSurface:
    """A `RoomSurface` that keeps what was said instead of talking to a homeserver."""

    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def system_prompt(self, session_id: UUID, room_id: str) -> str:
        return "you are Haku"

    async def deliver(self, room_id: str, text: str) -> None:
        assert room_id == ROOM
        self.delivered.append(text)

    async def report(self, room_id: str, detail: str) -> None:
        return None

    async def show_status(self, room_id: str, text: str) -> None:
        return None

    async def clear_status(self, room_id: str) -> None:
        return None

    async def set_typing(self, room_id: str, active: bool) -> None:
        return None


class _InterruptedCli(_FakeCli):
    """Aborts once its script has run out, and answers `interrupt` with a `result` frame — which
    is what a real CLI does and what the turn loop drains to.

    **Where the abort lands is the point.** A real one arrives between frames, with the turn
    parked on `anext`, so this fires it exactly there: when the loop asks for a frame that has
    not been sent. Set before the turn it would be a different case (nothing is ever spoken),
    and set from outside it would race the loop instead of landing at a known point — and one
    that lands while a frame is already in hand does not exercise the drain at all.
    """

    def __init__(self, script: list[dict[str, Any]], *, abort_event: asyncio.Event):
        super().__init__(script)
        self._abort_event = abort_event

    async def interrupt(self) -> None:
        await super().interrupt()
        self._queue.put_nowait(_result("stopped"))

    async def frames(self):
        source = super().frames()
        for _ in self.script:
            yield await anext(source)
        self._abort_event.set()
        async for frame in source:
            yield frame


async def _turn_into_a_room(
    chat_store: ClaudeChatStore,
    recording_claims: RecordingClaims,
    notifications: ChatNotifications,
    operator_id: UUID,
    client: _FakeCli,
    *,
    abort_event: asyncio.Event | None = None,
) -> list[str]:
    """Run one turn against *client* for a room-backed session and return what the room heard."""
    room = _RecordingRoomSurface()
    service = ClaudeChatService(
        runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN, room_surface=room
    )
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    async with asyncio.timeout(30):
        await service._run_turn(
            cast(Any, client),
            client.frames().__aiter__(),
            view.session_id,
            turn.prompt,
            turn_id=turn.turn_id,
            room_id=ROOM,
            abort_event=abort_event or asyncio.Event(),
        )
    return room.delivered


async def test_the_room_hears_each_assistant_message_as_it_finishes(
    chat_store, recording_claims, notifications, operator_id
) -> None:
    """A turn that says what it is about to do, works, then reports back is three messages in
    the transcript and used to be one in the room: it spoke only the final answer, so the room
    watched a long turn in silence and then saw a conclusion with none of its reasoning."""
    delivered = await _turn_into_a_room(
        chat_store, recording_claims, notifications, operator_id, _FakeCli(_NARRATED_TURN)
    )

    assert delivered == ["Looking at the logs now.", "Found it: a bad config."]


async def test_the_last_message_is_not_repeated_by_the_result_frame(
    chat_store, recording_claims, notifications, operator_id
) -> None:
    """`result.result` carries the same text as the turn's last assistant message, so speaking
    both would post the answer twice."""
    delivered = await _turn_into_a_room(
        chat_store, recording_claims, notifications, operator_id, _FakeCli(_NARRATED_TURN)
    )

    assert delivered.count("Found it: a bad config.") == 1


async def test_a_turn_whose_answer_arrived_only_on_the_result_is_still_spoken(
    chat_store, recording_claims, notifications, operator_id
) -> None:
    """No assistant message completed, so nothing was said along the way — the `result` frame is
    the only thing that keeps the room from hearing silence."""
    delivered = await _turn_into_a_room(
        chat_store, recording_claims, notifications, operator_id, _FakeCli([_result("nothing streamed, but an answer")])
    )

    assert delivered == ["nothing streamed, but an answer"]


async def test_an_aborted_turn_says_so_on_its_own(chat_store, recording_claims, notifications, operator_id) -> None:
    """Two things this pins down. The abort notice rides on `final_text`, which a turn that has
    already spoken no longer delivers, so it has to be said on its own or an operator's stop is
    invisible in the room. And the turn has to *survive* the abort at all: draining to the
    interrupt's `result` used to open a second `anext` on the session's generator, which an async
    generator refuses — so an abort landing where they land, between frames, raised out of the
    turn and failed the whole session instead of ending its turn.
    """
    abort_event = asyncio.Event()
    client = _InterruptedCli(_NARRATED_TURN[:-1], abort_event=abort_event)

    delivered = await _turn_into_a_room(
        chat_store, recording_claims, notifications, operator_id, client, abort_event=abort_event
    )

    assert client.interrupted
    # The notice on its own, and nothing from the interrupt's own `result` frame ("stopped").
    assert delivered == ["Looking at the logs now.", "Found it: a bad config.", ABORTED_NOTICE]


async def test_a_turn_brackets_the_frames_it_produced_and_keeps_what_it_cost(
    chat_store, chat_service, operator_id
) -> None:
    """The `result` frame's cost, usage and duration exist nowhere else — they were read for the
    error check and dropped — and the bracket is what makes them findable afterwards."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    # A frame from before this turn, so a bracket that started at the log's beginning would show.
    await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "system", {"type": "system"})
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli(
        [
            _assistant({"type": "text", "text": "a bad config"}),
            _result("a bad config", total_cost_usd=0.0125, duration_ms=4200, usage={"output_tokens": 91}),
        ]
    )

    await chat_service._run_turn(
        cast(Any, client),
        client.frames().__aiter__(),
        view.session_id,
        turn.prompt,
        turn_id=turn.turn_id,
        room_id=None,
        abort_event=asyncio.Event(),
    )
    # Recorded by the real socket wrapper in production; here the turn wrote none of its own, so
    # the upper bound is the frame that predates it and the range is honestly empty.
    [record] = await chat_store.list_turns(str(view.session_id), limit=10)

    assert record.outcome == TurnOutcome.ANSWERED
    assert record.cost_usd == 0.0125
    assert record.duration_ms == 4200
    assert record.usage == {"output_tokens": 91}
    assert record.first_frame_seq > record.last_frame_seq if record.last_frame_seq else True
    assert record.ended_at is not None


async def test_the_transcript_carries_what_each_tool_answered(chat_store, chat_service, operator_id) -> None:
    """`claude_chat_messages.tool_uses` keeps the `tool_use` blocks that asked and nothing that
    answered — the turn loop drops the `user` frames carrying results. The frames beside it hold
    both, so the view joins them by `tool_use_id`, which is exact where matching the Nth message to
    the Nth frame would be a guess."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "count the files")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    client = _FakeCli(
        [
            _assistant({"type": "tool_use", "id": "toolu_ok", "name": "Bash", "input": {"command": "true"}}),
            _assistant({"type": "tool_use", "id": "toolu_running", "name": "Bash", "input": {"command": "sleep 1"}}),
            _result("done"),
        ]
    )
    await chat_service._run_turn(
        cast(Any, client),
        client.frames().__aiter__(),
        view.session_id,
        turn.prompt,
        turn_id=turn.turn_id,
        room_id=None,
        abort_event=asyncio.Event(),
    )
    # As the CLI sends them: a result is a `user` frame, and one call is left unanswered.
    await chat_store.record_frame(
        view.session_id,
        FrameDirection.FROM_AGENT,
        "user",
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_ok", "content": "42", "is_error": False}],
            },
        },
    )

    calls = {
        call.tool_use_id: call
        for message in (await chat_store.get(operator_id, view.session_id)).messages
        for call in message.tool_uses
    }

    assert calls["toolu_ok"].result is not None
    assert (calls["toolu_ok"].result.content, calls["toolu_ok"].result.is_error) == ("42", False)
    assert calls["toolu_running"].result is None, "a call still running must not read as an empty answer"


async def test_the_calls_come_from_the_rollout_when_the_row_points_at_it(
    chat_store, chat_service, migrated_sessions, operator_id
) -> None:
    """The transcript row records the agent's own message id, which is the pointer the message
    rows never had — so a message finds exactly the calls it made instead of the view matching by
    position. `tool_uses` is then a copy nothing reads for such a row."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "count the files")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    asked = {"type": "tool_use", "id": "toolu_ok", "name": "Bash", "input": {"command": "true"}}
    frame = _assistant(asked) | {"message": {"role": "assistant", "id": "msg_01", "content": [asked]}}
    client = _FakeCli([frame, _result("done")])
    await chat_service._run_turn(
        cast(Any, client),
        client.frames().__aiter__(),
        view.session_id,
        turn.prompt,
        turn_id=turn.turn_id,
        room_id=None,
        abort_event=asyncio.Event(),
    )
    # The frames the recorder would have written, plus the answer, which is a `user` frame.
    await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, frame["type"], frame)
    await chat_store.record_frame(
        view.session_id,
        FrameDirection.FROM_AGENT,
        "user",
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_ok", "content": "7"}],
            },
        },
    )
    # Whatever the column says is now beside the point, so make it say something wrong.
    async with migrated_sessions.begin() as db:
        for message in await db.scalars(
            select(ClaudeChatMessage).where(ClaudeChatMessage.agent_message_id == "msg_01")
        ):
            message.tool_uses = [{"tool_use_id": "toolu_stale", "name": "Stale", "input": {}}]

    [call] = [
        call for message in (await chat_store.get(operator_id, view.session_id)).messages for call in message.tool_uses
    ]

    assert (call.tool_use_id, call.name) == ("toolu_ok", "Bash"), "the rollout wins over the column"
    assert call.result is not None
    assert call.result.content == "7"


async def test_a_message_with_nothing_to_point_at_still_reads_its_calls_from_the_column(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A row written before the pointer existed, or one the console synthesized rather than
    observed — a turn whose text arrived only on the `result` frame — has no agent message id, and
    the column is all it has. That is why the column is still written."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    message_id = await chat_store.begin_assistant(view.session_id)
    await chat_store.update_assistant(
        view.session_id,
        message_id,
        "did a thing",
        tool_uses=[{"tool_use_id": "toolu_legacy", "name": "Bash", "input": {}}],
        complete=True,
    )

    [call] = [
        call for message in (await chat_store.get(operator_id, view.session_id)).messages for call in message.tool_uses
    ]

    assert call.tool_use_id == "toolu_legacy"
    async with migrated_sessions() as db:
        assert (
            await db.scalar(
                select(ClaudeChatMessage.agent_message_id).where(ClaudeChatMessage.message_id == message_id)
            )
            is None
        )


async def test_a_second_prompt_is_refused_while_a_turn_is_open(chat_store, operator_id) -> None:
    """The gate `enqueue_prompt` used to keep was `status == READY`, which doubled as "not
    mid-turn" only because `enqueue_prompt` itself had written `responding`. Asking the turn
    directly is what keeps R2.2 — hold a batch until the turn ends — from silently becoming
    fold-into-turn with no fold path wired.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None

    with pytest.raises(RuntimeError, match="turn is already in flight"):
        await chat_store.enqueue_prompt(operator_id, view.session_id, "second")

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "second")


async def test_a_prompt_is_taken_off_the_queue_rather_than_found_by_status(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The transcript row used to be the queue: `COMPLETE` on a user row meant "handed to the
    model" while on an assistant row it means "the answer finished". The queue row is what says a
    prompt is waiting now, and claiming it is what says it no longer is."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")

    async with migrated_sessions() as db:
        queued = list(await db.scalars(select(ClaudeChatPrompt)))
    assert [(row.session_id, row.claimed_at) for row in queued] == [(view.session_id, None)]

    turn = await chat_store.next_prompt(view.session_id)

    assert turn is not None
    assert turn.prompt == "why did it fail?", "the text comes from the transcript row the queue names"
    async with migrated_sessions() as db:
        [claimed] = list(await db.scalars(select(ClaudeChatPrompt)))
    assert claimed.claimed_at is not None
    assert claimed.message_id == turn.message_id


async def test_one_prompt_in_flight_is_a_schema_property(chat_store, migrated_sessions, operator_id) -> None:
    """It used to be a scan of the transcript for a `pending` row plus the rule that only one
    exists — so two replicas racing on one session could each conclude they may accept."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")

    async with migrated_sessions() as db:
        message = ClaudeChatMessage(
            message_id=uuid4(),
            session_id=view.session_id,
            role=ChatMessageRole.USER,
            status=ChatMessageStatus.PENDING,
            content="second",
            tool_uses=[],
            error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(message)
        db.add(
            ClaudeChatPrompt(
                prompt_id=uuid4(),
                session_id=view.session_id,
                message_id=message.message_id,
                queued_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_a_prompt_from_a_replica_that_wrote_no_queue_row_is_still_answered(
    chat_store, migrated_sessions, operator_id
) -> None:
    """During the roll that adds the queue, a prompt an old replica accepted exists only as a
    `pending` message row. Dropping that scan now would leave it accepted and never answered."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    async with migrated_sessions.begin() as db:
        db.add(
            ClaudeChatMessage(
                message_id=uuid4(),
                session_id=view.session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.PENDING,
                content="enqueued by the previous image",
                tool_uses=[],
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    turn = await chat_store.next_prompt(view.session_id)

    assert turn is not None
    assert turn.prompt == "enqueued by the previous image"
    # And admission still refuses while it waits, so the two paths cannot both accept.
    async with migrated_sessions.begin() as db:
        db.add(
            ClaudeChatMessage(
                message_id=uuid4(),
                session_id=view.session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.PENDING,
                content="another legacy one",
                tool_uses=[],
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    with pytest.raises(RuntimeError, match="already queued"):
        await chat_store.enqueue_prompt(operator_id, view.session_id, "mine")


async def test_a_matrix_batch_offered_mid_turn_is_still_held(
    chat_store, conversations, migrated_identity_store, operator_id
) -> None:
    """The homeserver re-delivers what `offer` refuses, so refusing is how a message sent while
    Haku is working waits for the next turn instead of being answered a turn late."""
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=MATRIX_ROOM))
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    assert await conversations.claim_room(MATRIX_USER, MATRIX_ROOM) == MATRIX_ROOM
    await conversations.set_session(MATRIX_USER, view.session_id)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")
    assert await chat_store.next_prompt(view.session_id) is not None
    turns = MatrixTurns(MATRIX_CONFIG, conversations, chat_store, migrated_identity_store)

    offered = await turns.offer(
        [
            InboundMessage(
                room_id=MATRIX_ROOM, event_id="$2", sender=MATRIX_OPERATOR, body="and another thing", origin_server_ts=2
            )
        ]
    )

    assert offered is False


async def test_the_view_says_responding_for_as_long_as_the_turn_is_open(chat_store, operator_id) -> None:
    """`status` is the SPA's contract, so the column underneath can stop carrying turn state
    without a frontend release — the view derives it from the open turn instead."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert (await chat_store.get(operator_id, view.session_id)).status == ChatSessionStatus.READY, (
        "a queued prompt is not a turn in flight"
    )

    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert (await chat_store.get(operator_id, view.session_id)).status == ChatSessionStatus.RESPONDING
    assert await chat_store.status(view.session_id) == ChatSessionStatus.READY, (
        "the column itself no longer carries turn state"
    )

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    assert (await chat_store.get(operator_id, view.session_id)).status == ChatSessionStatus.READY


async def test_a_session_that_ended_does_not_report_a_turn_it_left_open(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A replica losing its pod mid-turn closes nothing, so the open row is exactly the record of
    an abandoned exchange — and must not make a failed session read as still working."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.next_prompt(view.session_id) is not None
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 1

    [record] = await chat_store.list_turns(str(view.session_id), limit=10)
    assert record.ended_at is None, "nothing ran to close it, and the record should say so"
    assert (await chat_store.get(operator_id, view.session_id)).status == ChatSessionStatus.FAILED


class _RealDbClaudeClient(_LifecycleClaudeClient):
    """Answers every prompt with "pong", then goes quiet like an idle CLI."""

    def __init__(self, adapter: object, launch: object, on_progress: object = None, frames_to: object = None):
        super().__init__(adapter, launch, on_progress)
        self.script = [_assistant({"type": "text", "text": "pong"}), _result("pong")]


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
    view, token = await chat_store.create(operator_id, SpaSession())

    with patch("haku.console.x.claude_chat.cli_over_websocket", _RealDbClaudeClient):
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
            # test must observe it from outside instead of being handed a signal by it. What it
            # polls for is the closed turn — the session's status stays `ready` throughout now,
            # so waiting on that would be a wait for something already true.
            await chat_store.enqueue_prompt(operator_id, view.session_id, "ping")
            for _ in range(75):
                if [turn for turn in await chat_store.list_turns(str(view.session_id), limit=2) if turn.ended_at]:
                    break
                await asyncio.sleep(0.2)
            [turn] = await chat_store.list_turns(str(view.session_id), limit=2)
            assert turn.outcome == TurnOutcome.ANSWERED, "the turn never completed"
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    [answer] = [
        m for m in (await chat_store.get(operator_id, view.session_id)).messages if m.role == ChatMessageRole.ASSISTANT
    ]
    assert answer.content == "pong"


async def test_abort_is_refused_until_a_turn_is_actually_running(chat_store, operator_id) -> None:
    """An idle session has nothing to interrupt, and saying so is the point of the 409.

    A *queued* prompt is not a turn either, and this is where that used to go wrong twice over:
    the first check asked "is this session's abort event registered in this process", true for
    the whole life of the runner bridge; the second asked whether the session's status was
    `responding`, which `enqueue_prompt` set before any turn started. Both accepted an abort
    with nothing to abort, and the event then sat set until the next turn, killing it on
    arrival. The abort now names the open turn, which does not exist until the prompt is handed
    to the model.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    # The bridge handshake is what takes a session from provisioning to ready, and only a
    # ready session accepts a prompt.
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await chat_store.request_abort(view.session_id) is False

    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.request_abort(view.session_id) is False, "a queued prompt is not a turn"

    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert await chat_store.request_abort(view.session_id) is True

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    assert await chat_store.request_abort(view.session_id) is False


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
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.next_prompt(view.session_id) is not None, "the turn the abort names"

    other_engine = create_async_engine(migrated_db_url, pool_pre_ping=True)
    try:
        requesting = ClaudeChatStore(async_sessionmaker(other_engine, expire_on_commit=False))
        async with notifications.subscribe(ChatEventKind.ABORT, view.session_id) as aborted:
            assert await requesting.request_abort(view.session_id) is True
            async with asyncio.timeout(30):
                await aborted.wait()
    finally:
        await other_engine.dispose()


async def test_a_session_records_the_surface_it_was_created_for(chat_store, migrated_sessions, operator_id) -> None:
    """Which surface a conversation belonged to has to outlive the conversation.

    `matrix_conversation` holds one binding, so before this the room link vanished the moment
    the supervisor replaced a session, and a past Matrix session read as an SPA one.
    """
    spa, _ = await chat_store.create(operator_id, SpaSession())
    matrix, _ = await chat_store.create(operator_id, MatrixSession(room_id="!room:allegedly.works"))

    async with migrated_sessions() as db:
        assert (await db.get(ClaudeChatSession, spa.session_id)).surface == ChatSurface.SPA
        assert (await db.get(ClaudeChatSession, spa.session_id)).room_id is None
        assert (await db.get(ClaudeChatSession, matrix.session_id)).surface == ChatSurface.MATRIX
        assert (await db.get(ClaudeChatSession, matrix.session_id)).room_id == "!room:allegedly.works"


async def test_a_room_cannot_be_recorded_without_the_matrix_surface(migrated_sessions, operator_id) -> None:
    """The pairing is a schema rule, not only a call-signature one — the columns outlive it."""
    async with migrated_sessions.begin() as db:
        db.add(
            ClaudeChatSession(
                session_id=uuid4(),
                operator_id=operator_id,
                surface=ChatSurface.SPA,
                room_id="!room:allegedly.works",
                status=ChatSessionStatus.PROVISIONING,
                bridge_token_fingerprint=b"x" * 32,
                bridge_connected_at=None,
                error=None,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


class _ScriptedChannel:
    """A `FrameChannel` whose far end is a queue of the CLI's own frames."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self._inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def deliver(self, frame: dict[str, Any]) -> None:
        self._inbound.put_nowait(frame)

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        self.written.append(json.loads(data))

    async def read_messages(self):
        while (frame := await self._inbound.get()) is not None:
            yield frame

    async def close(self) -> None:
        self._inbound.put_nowait(None)


async def _frames(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> list[ClaudeChatFrame]:
    async with sessions() as db:
        return list(
            await db.scalars(
                select(ClaudeChatFrame)
                .where(ClaudeChatFrame.session_id == session_id)
                .order_by(ClaudeChatFrame.frame_seq)
            )
        )


async def test_the_rollout_records_both_channels_both_ways_and_skips_only_deltas(
    chat_store, migrated_sessions, operator_id
) -> None:
    """What the agent did is only recoverable from the wire.

    Tool results arrive as `user` frames, which the turn loop drops entirely — it keeps the
    `tool_use` blocks that asked and nothing that answered — so the record is taken where every
    frame passes rather than from what the loop unpacks. **The control channel counts.** It never
    reaches `frames()`, so recording off the conversation queue would drop `interrupt` and its
    answer from the log, and an interrupt that did not take is diagnosable from nothing else.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    tool_result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "42"}]}}
    channel = _ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5, frames_to=RolloutRecorder(chat_store, view.session_id))

    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    initialize = channel.written[0]
    channel.deliver(
        {"type": "control_response", "response": {"subtype": "success", "request_id": initialize["request_id"]}}
    )
    await connecting
    await cli.query("what did that return?")
    channel.deliver({"type": "stream_event", "event": {"type": "content_block_delta"}})
    channel.deliver(tool_result)
    # Reading is what proves the reader got that far; the recorder runs inside it. Deltas do
    # reach a reader — only the record skips them.
    frames = cli.frames()
    assert (await anext(frames))["type"] == "stream_event"
    assert await anext(frames) == tool_result
    await cli.aclose()

    recorded = await _frames(migrated_sessions, view.session_id)
    assert [(frame.direction, frame.kind) for frame in recorded] == [
        (FrameDirection.TO_AGENT, "control_request"),
        (FrameDirection.FROM_AGENT, "control_response"),
        (FrameDirection.TO_AGENT, "user"),
        (FrameDirection.FROM_AGENT, "user"),
    ]
    # Verbatim: a reader gets the tool result the turn loop never kept.
    assert recorded[3].payload == tool_result
    assert all(frame.partial is False for frame in recorded)


def _text_delta_frame(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


class _DyingMidStreamClaudeClient(_LifecycleClaudeClient):
    """Streams two deltas, then ends the turn without ever completing the message."""

    def __init__(self, adapter: object, launch: object, on_progress: object = None, frames_to: object = None):
        super().__init__(adapter, launch, on_progress)
        self.script = [_text_delta_frame("half an "), _text_delta_frame("answer"), _result()]


async def test_an_answer_cut_off_mid_stream_is_in_the_rollout(
    chat_store, chat_service, migrated_sessions, operator_id
) -> None:
    """Written as it streams, not reconstructed at the end, because the end may never come.

    The deltas are not kept as frames, so an interrupted turn would otherwise stop mid-answer
    in the log — and reconstructing it in a finalizer would miss the case worth having, since
    a replica losing its pod raises `CancelledError` straight past one.
    """
    view, token = await chat_store.create(operator_id, SpaSession())

    with patch("haku.console.x.claude_chat.cli_over_websocket", _DyingMidStreamClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        try:
            for _ in range(75):
                if await chat_store.status(view.session_id) == ChatSessionStatus.READY:
                    break
                await asyncio.sleep(0.2)
            await chat_store.enqueue_prompt(operator_id, view.session_id, "go")
            # Waits for the streamed text, not merely for a partial row to exist. The first
            # delta creates the row, so waiting on its existence raced the second delta and
            # cancelled between them — which asserted a timing rather than the property. What
            # makes this "cut off mid-stream" is that no `assistant` frame ever completes the
            # message, and that is true however many deltas have landed.
            for _ in range(75):
                partial = [f for f in await _frames(migrated_sessions, view.session_id) if f.partial]
                if partial and partial[0].payload["message"]["content"][0]["text"] == "half an answer":
                    break
                await asyncio.sleep(0.2)
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner

    [reconstructed] = [f for f in await _frames(migrated_sessions, view.session_id) if f.partial]
    assert reconstructed.kind == "assistant"
    assert reconstructed.payload["message"]["content"][0]["text"] == "half an answer"


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
    view, _ = await chat_store.create(operator_id, SpaSession())
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 1
    assert await chat_store.status(view.session_id) == ChatSessionStatus.FAILED
    assert "went away" in (await chat_store.get(operator_id, view.session_id)).error


async def test_an_unheld_session_says_no_replica_ever_attached(chat_store, migrated_sessions, operator_id) -> None:
    """The creator's provisioning grant has no holder, so a sandbox that never came up must not
    blame a replica for going away."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 1
    assert "never attached" in (await chat_store.get(operator_id, view.session_id)).error


async def test_a_failed_session_names_the_replica_that_held_it(chat_store, migrated_sessions, operator_id) -> None:
    """The whole reason to record a holder: this message used to be identical for every such
    failure, so a room said a session died and nothing could say which process to go read."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 1
    assert REPLICA in (await chat_store.get(operator_id, view.session_id)).error


async def test_renewing_is_what_claims_the_session(chat_store, migrated_sessions, operator_id) -> None:
    """A session goes from budgeted to held the first time its replica renews, with nothing else
    sequencing the handover."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    async with migrated_sessions() as db:
        assert (await db.get(ClaudeChatSession, view.session_id)).lease_holder is None

    await chat_store.renew_lease(view.session_id)

    async with migrated_sessions() as db:
        assert (await db.get(ClaudeChatSession, view.session_id)).lease_holder == REPLICA


async def test_a_session_whose_holder_is_still_renewing_is_left_alone(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A busy replica must not have its session reclaimed out from under it."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)

    assert await chat_store.expire_stale_leases() == 0
    assert await chat_store.status(view.session_id) == ChatSessionStatus.PROVISIONING


async def test_an_ended_session_is_not_reclassified_by_the_sweep(chat_store, migrated_sessions, operator_id) -> None:
    """Only a *live* status is a lie worth correcting; a terminal one is already the truth."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.fail(view.session_id, "something else went wrong first")
    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 0
    assert (await chat_store.get(operator_id, view.session_id)).error == "something else went wrong first"


async def test_a_cancelled_runner_hands_the_session_back_without_stranding_it(
    chat_store, chat_service, migrated_sessions, operator_id
) -> None:
    """Pod termination cancels this task, and `CancelledError` is not an `Exception`.

    So neither `except` clause saw it, and the session kept a live status nobody was
    maintaining — the room waited forever. Failing the row closed that hole and cost every roll
    its conversation, because a failed session is terminal and the runner's reconnect is refused.

    Handing it back keeps both properties: the session stays adoptable by whichever replica the
    runner reaches, and the short grant left behind is what the sweep still catches when no
    runner ever does.
    """
    view, token = await chat_store.create(operator_id, SpaSession())

    with patch("haku.console.x.claude_chat.cli_over_websocket", _RealDbClaudeClient):
        runner = asyncio.create_task(
            chat_service.handle_runner(cast(Any, _LifecycleWebSocket()), view.session_id, token)
        )
        await asyncio.sleep(2)  # Long enough to reach the idle wait, as the sibling test does.
        assert await chat_store.status(view.session_id) == ChatSessionStatus.READY

        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert await chat_store.status(view.session_id) == ChatSessionStatus.READY
    with patch("haku.console.x.claude_chat.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    await _age_lease(migrated_sessions, view.session_id, seconds_ago=1)
    assert await chat_store.expire_stale_leases() == 1
    assert await chat_store.status(view.session_id) == ChatSessionStatus.FAILED


if __name__ == "__main__":
    pytest_bazel.main()
