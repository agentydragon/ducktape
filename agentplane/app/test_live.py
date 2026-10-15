"""The pushed views against the fetched ones, the staleness verdict, the frames a tab reads, and
what a sandbox frame says when the Action Service cannot be asked."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_federation_settings import DirectFederationSettings
from agentplane.app.action_policy import ActionPolicyInventory, ActionPolicyUnavailable, ActionPolicyView
from agentplane.app.api import app_services, create_app
from agentplane.app.conftest import stored_login
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import CallerIdentity, CallerKind, TokenReviewer
from agentplane.app.live import (
    PODS_PLURAL,
    ActionPolicyFrames,
    DecisionsSnapshot,
    DecisionsUnavailable,
    LiveIndex,
    SandboxesSnapshot,
    ThreadsSnapshot,
    ThreadsUnavailable,
    WatchHealth,
    action_policy_frame,
    frames,
    live_egress_decisions,
    live_threads,
    threads_frame,
)
from agentplane.app.oidc import OIDCSettings
from agentplane.app.operator_sessions import (
    LoginTokens,
    OperatorSession,
    OperatorSessionStore,
    RequestSession,
    SessionRow,
)
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.sandbox_models import sandbox_view
from agentplane.app.shutdown import Drain
from agentplane.app.testing.dispatcher import GatewayBackedDispatcher
from agentplane.app.testing.egress_proxy import FakeEgressAdmin, decision
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.testing.thread_test_support import Replica
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.events import event_log
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.postgres.wakeups import WakeupListener
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient
from agentplane.sandbox_resource import SANDBOXES_PLURAL, OperatingMode
from agentplane.sandboxes.testing.fake_inventory import (
    NAMESPACE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    egress_policy_set,
    policy_binding,
    sandbox,
    seed_runner,
)
from agentplane.subjects import ServiceAccountRef
from util.net import pick_free_port

# gazelle:include_dep @pypi//protobuf

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def seeded(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api, live_index: LiveIndex) -> LiveIndex:
    """The same objects in the fake API server and in the index, so the two paths can be compared."""
    seed_runner(custom_objects, core_v1, "runner-1")
    custom_objects.objects[(SANDBOXES_PLURAL, "shelved")] = sandbox("shelved", running=False)
    custom_objects.objects[("policysets", "github")] = egress_policy_set(
        "github", [{"hosts": ["api.github.com"], "methods": ["GET"]}]
    )
    custom_objects.objects[("policybindings", "runner-1-picked")] = policy_binding(
        "runner-1-picked", subject={"namespace": NAMESPACE, "name": "runner-1"}, policy_sets=["github"]
    )
    custom_objects.objects[("policybindings", "elsewhere")] = policy_binding(
        "elsewhere", subject={"namespace": NAMESPACE, "name": "shelved"}, policy_sets=["github"]
    )
    for (kind, name), obj in custom_objects.objects.items():
        match kind:
            case "sandboxes":
                live_index.sandboxes[name] = obj
            case "policybindings":
                live_index.bindings[name] = obj
            case "policysets":
                live_index.rule_sets[name] = obj
    live_index.pods.update(core_v1.pods)
    return live_index


async def test_the_index_projects_the_rows_a_listing_would_return(seeded: LiveIndex, inventory: SandboxAccess) -> None:
    """The push and the fetch share their projection; this is what says they still do."""
    assert seeded.sandbox_views() == [sandbox_view(view) for view in await inventory.list_sandboxes()]
    assert seeded.sandbox_view("runner-1") == sandbox_view(await inventory.get("runner-1"))


async def test_the_index_selects_the_bindings_a_request_would(seeded: LiveIndex, egress: EgressAccess) -> None:
    runner = ServiceAccountRef(namespace=NAMESPACE, name="runner-1")
    assert seeded.bindings_for(runner) == await egress.bindings_for(runner)
    assert [binding.name for binding in seeded.bindings_for(runner)] == ["runner-1-picked"]


EMPTY_POLICY = ActionPolicyView(synced=True, bindings=[], auto_approve_if=[])


async def test_the_sandbox_stream_asks_the_service_again_only_when_a_policy_object_changed(seeded: LiveIndex) -> None:
    """Every other change -- a Pod, a thread -- repeats the last answer; a failure is never repeated,
    and a different sandbox is asked about on its own."""
    answers: list[ActionPolicyView | ActionPolicyUnavailable] = [
        ActionPolicyUnavailable(code="test-first-attempt-failed"),
        EMPTY_POLICY,
        EMPTY_POLICY.model_copy(update={"synced": False}),
        EMPTY_POLICY,
    ]
    asked: list[ServiceAccountRef] = []

    async def fetch(subject: ServiceAccountRef) -> ActionPolicyView | ActionPolicyUnavailable:
        asked.append(subject)
        return answers.pop(0)

    runner = ServiceAccountRef(namespace=NAMESPACE, name="runner-1")
    other = ServiceAccountRef(namespace=NAMESPACE, name="shelved")
    policy = ActionPolicyFrames(seeded, fetch)

    assert await policy.for_subject(runner) == ActionPolicyUnavailable(code="test-first-attempt-failed")
    assert await policy.for_subject(runner) == EMPTY_POLICY  # retried: the failure was not kept
    assert await policy.for_subject(runner) == EMPTY_POLICY  # nothing changed: repeated, not asked
    seeded.policy_seen(seeded.bindings, "elsewhere", None)
    assert await policy.for_subject(runner) == EMPTY_POLICY.model_copy(update={"synced": False})
    assert await policy.for_subject(other) == EMPTY_POLICY
    assert asked == [runner, runner, runner, other]


def _request(app: FastAPI, login: OperatorSession, row: SessionRow) -> Request:
    """A request as the session middleware hands it on: its session holding the login, and the row
    it came from."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/live/sandboxes/runner-1",
            "headers": [],
            "app": app,
            "state": {"request_session": RequestSession({}, login, row)},
        }
    )


@pytest.mark.parametrize(
    ("caller", "configured", "code"),
    [
        (CallerIdentity(CallerKind.TOKEN, "test-agent"), True, "operator_session_required"),
        (CallerIdentity(CallerKind.OPERATOR, "test-operator"), False, "operator_federation_not_configured"),
        (CallerIdentity(CallerKind.OPERATOR, "test-operator"), True, "operator_federation_token_invalid"),
    ],
)
async def test_a_policy_the_service_cannot_be_asked_for_is_said_so_in_the_frame(
    app: FastAPI,
    action_policy: ActionPolicyInventory,
    operator_sessions: OperatorSessionStore,
    caller: CallerIdentity,
    configured: bool,
    code: str,
) -> None:
    """The failure the route would answer with, as a frame variant a tab can show: a token caller
    is not an operator, an app without federation has nobody to ask as, and a session whose token
    the identity service refuses is refused with that code and nothing of its own text."""
    oidc = OIDCSettings(
        issuer="https://login.test.invalid/application/o/test-app/",
        client_id="test-app",
        client_secret="test-only-client-secret",
        session_secret="test-only-session-secret",
        public_base_url="http://test-app.invalid",
    )
    # An app with a login, whose session middleware `_request` stands in for.
    app_services(app).oidc = oidc
    app_services(app).operator_actions = (
        FederatedOperatorActions(
            DirectFederationSettings(
                service_url=AnyHttpUrl("http://test-actions.invalid"),
                login_jwks_uri=f"http://127.0.0.1:{pick_free_port()}/jwks",
                target=OperatorOidcSettings(
                    issuer="https://login.test.invalid/application/o/actions/",
                    audience="test-actions",
                    jwks_uri=f"http://127.0.0.1:{pick_free_port()}/jwks",
                ),
                scope="openid",
            ),
            oidc,
            httpx.AsyncClient(base_url="http://test-actions.invalid"),
        )
        if configured
        else None
    )
    login = OperatorSession(
        issuer=oidc.issuer,
        subject="test-operator-subject",
        username="test-operator",
        tokens=LoginTokens(
            access_token=SecretStr("test-not-a-jwt"),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            refresh_token=None,
        ),
    )
    row = await stored_login(operator_sessions, login)

    frame = await action_policy_frame(
        _request(app, login, row), caller, action_policy, ServiceAccountRef(namespace=NAMESPACE, name="runner-1")
    )

    assert frame == ActionPolicyUnavailable(code=code)


async def test_threads_the_gateway_cannot_list_are_said_so_in_the_frame(
    engine: AsyncEngine, summaries: Summaries, tmp_path: Path
) -> None:
    """The sandbox stream still carries the sandbox, and so its lifecycle controls, when the Runner
    Gateway cannot say which Sessions run in it."""
    (token := tmp_path / "token").write_text("test-token")
    port = pick_free_port()
    async with dial(f"127.0.0.1:{port}") as channel:
        unreachable = RunnerGatewayClient(
            channel,
            token_file=token,
            command_admission_timeout_s=5,
            request_timeout_s=5,
            control=SandboxControlClient(
                control_target=f"127.0.0.1:{port}", token_file=token, request_timeout_s=5, lifecycle_timeout_s=5
            ),
        )
        frame = await threads_frame(
            ThreadStore(engine, summaries, gateway=unreachable), "runner-1", include_archived=False
        )
    assert frame == ThreadsUnavailable(code="UNAVAILABLE")


def test_a_sandbox_the_watch_has_dropped_is_gone_rather_than_missing(seeded: LiveIndex) -> None:
    del seeded.sandboxes["runner-1"]

    assert seeded.sandbox_view("runner-1") is None


def test_nothing_watched_yet_reads_as_stale(live_index: LiveIndex) -> None:
    """A process whose first cycle never completed must not read as fresh for having no age."""
    assert live_index.health(NOW).fresh is False


def test_one_kind_that_stopped_cycling_makes_the_whole_frame_stale(live_index: LiveIndex) -> None:
    live_index.freshness.at[SANDBOXES_PLURAL] = NOW - timedelta(seconds=5)
    live_index.freshness.at[PODS_PLURAL] = NOW - timedelta(seconds=5)
    assert live_index.health(NOW).fresh is True

    live_index.freshness.at[PODS_PLURAL] = NOW - timedelta(seconds=live_index.freshness.stale_after_seconds + 1)
    health = live_index.health(NOW)

    assert health.fresh is False
    assert health.refreshed_seconds_ago[PODS_PLURAL] > health.stale_after_seconds


async def test_a_frame_goes_out_per_change_with_health_through_the_quiet(seeded: LiveIndex) -> None:
    """What a tab reads: the state on connect, the state again when it changes, and in between the
    freshness that tells a quiet stream from a wedged one."""
    seeded.freshness.at[SANDBOXES_PLURAL] = NOW
    stream = frames(lambda: _snapshot(seeded), lambda: seeded.health(NOW), seeded.changes, interval_s=0.01).__aiter__()

    assert _read(await anext(stream)) == ("snapshot", ["runner-1", "shelved"])
    assert _read(await anext(stream))[0] == "health"
    del seeded.sandboxes["runner-1"]
    seeded.changes.notify()

    assert await asyncio.wait_for(_next_snapshot(stream), timeout=5) == ["shelved"]


@pytest.fixture
def app(
    inventory: SandboxAccess,
    gateway: RunnerGatewayClient,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    dispatcher: GatewayBackedDispatcher,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    tmp_path: Path,
    electric_http: httpx.AsyncClient,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> FastAPI:
    """Neither test below reaches a database, a runner or the History Service -- the guard answers
    before a route body runs, and the document comes from the signatures -- so nothing here connects."""
    engine = connect("postgresql+asyncpg://live-test@127.0.0.1:1/live-test")
    history = HistoryServiceClient("127.0.0.1:1", token_file=tmp_path / "unused-history-token", request_timeout_s=1)
    summaries = Summaries(history)
    event_logs, content = (
        event_log.EventLogStore(engine, history_reader=history, summaries=summaries),
        ContentStore(engine, history_reader=history),
    )
    database_updates = create_database_updates(engine.url)
    return create_app(
        inventory,
        RunnerBridge(
            runners=RunnerDirectory(live_index, gateway), event_logs=event_logs, content=content, dispatcher=dispatcher
        ),
        ThreadStore(engine, summaries, gateway=gateway),
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=OperatorSessionStore(engine),
        electric=ElectricProxy(
            electric_http, content, event_logs=event_logs, thread_changes=database_updates.changes[Channel.THREADS]
        ),
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
        system_status=answering_system_status(),
    )


def test_the_streams_need_a_caller(app: FastAPI) -> None:
    with TestClient(app) as unauthenticated:
        assert unauthenticated.get("/live/sandboxes").status_code == 401
        assert unauthenticated.get("/live/sandboxes/runner-1/egress/decisions").status_code == 401
        assert unauthenticated.get("/live/sandboxes/runner-1").status_code == 401
        assert unauthenticated.get("/live/threads").status_code == 401


async def _next_threads(stream: AsyncIterator[str | bytes | memoryview]) -> ThreadsSnapshot:
    while True:
        frame = await anext(stream)
        assert isinstance(frame, bytes)
        if frame.startswith(b"event: snapshot\n"):
            return ThreadsSnapshot.model_validate_json(frame.partition(b"data: ")[2])


async def test_global_thread_stream_combines_replica_commits_and_sandbox_watch_changes(
    seeded: LiveIndex,
    store: ThreadStore,
    event_logs: SeededEventLogStore,
    replica: Replica,
    database_updates: WakeupListener,
) -> None:
    drain = Drain()
    response = await live_threads(
        index=seeded, store=replica.store, updates=database_updates, shutdown=drain, include_archived=True
    )
    stream = aiter(response.body_iterator)
    try:
        async with asyncio.timeout(10):
            initial = await _next_threads(stream)
            assert {sandbox.name for sandbox in initial.sandboxes} == {"runner-1", "shelved"}
            assert initial.threads == []
            assert initial.updates_connected

            thread_id = await event_logs.seed("runner-1", "test-global-thread")
            assert [thread.id for thread in (await _next_threads(stream)).threads] == [thread_id]
            await store.rename(thread_id, "Renamed in another app replica")
            assert (await _next_threads(stream)).threads[0].name == "Renamed in another app replica"
            await store.archive(thread_id)
            archived = await _next_threads(stream)
            assert (archived.threads[0].archived, archived.archived_count) == (True, 1)

            seeded.sandboxes["runner-1"] = sandbox("runner-1", running=False)
            seeded.changes.notify()
            suspended = await _next_threads(stream)
            assert next(view for view in suspended.sandboxes if view.name == "runner-1").operating_mode == (
                OperatingMode.SUSPENDED
            )
            del seeded.sandboxes["runner-1"]
            seeded.changes.notify()
            deleted = await _next_threads(stream)
            assert [view.name for view in deleted.sandboxes] == ["shelved"]
            assert [thread.id for thread in deleted.threads] == [thread_id]
    finally:
        drain.begin()
        async for _ in stream:
            pass


async def test_global_thread_stream_carries_its_window_and_the_open_thread(
    seeded: LiveIndex, event_logs: SeededEventLogStore, replica: Replica, database_updates: WakeupListener
) -> None:
    oldest, _, newest = [await event_logs.seed("runner-1", f"test-window-thread-{n}") for n in range(3)]
    drain = Drain()
    response = await live_threads(
        index=seeded, store=replica.store, updates=database_updates, shutdown=drain, limit=1, open_thread=oldest
    )
    stream = aiter(response.body_iterator)
    try:
        async with asyncio.timeout(10):
            window = await _next_threads(stream)
            assert ([thread.id for thread in window.threads], window.more) == ([newest, oldest], True)
    finally:
        drain.begin()
        async for _ in stream:
            pass


async def test_global_thread_stream_reports_listener_loss_then_rereads_after_reconnect(
    seeded: LiveIndex, event_logs: SeededEventLogStore, replica: Replica, database_updates: WakeupListener, db_url: str
) -> None:
    drain = Drain()
    response = await live_threads(index=seeded, store=replica.store, updates=database_updates, shutdown=drain)
    stream = aiter(response.body_iterator)
    engine = create_async_engine(db_url)
    try:
        async with asyncio.timeout(10):
            assert (await _next_threads(stream)).updates_connected
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND application_name = 'agentplane-database-updates'"
                    )
                )
            assert not (await _next_threads(stream)).updates_connected
            thread_id = await event_logs.seed("runner-1", "test-during-listener-gap")
            # A History Service fold notice may send a snapshot before the listener reconnects.
            while not (recovered := await _next_threads(stream)).updates_connected:
                pass
            assert [thread.id for thread in recovered.threads] == [thread_id]
    finally:
        drain.begin()
        async for _ in stream:
            pass
        await engine.dispose()


async def _snapshot(index: LiveIndex) -> SandboxesSnapshot:
    return SandboxesSnapshot(sandboxes=index.sandbox_views(), watch=index.health(NOW))


def _read(frame: bytes) -> tuple[str, object]:
    """One SSE frame as (event, the sandbox names it carries or its health)."""
    event, data = (line.split(": ", 1)[1] for line in frame.decode().strip().splitlines())
    payload = json.loads(data)
    if event == "health":
        return event, WatchHealth.model_validate(payload)
    return event, [row["name"] for row in payload["sandboxes"]]


async def _next_snapshot(stream: AsyncIterator[bytes]) -> object:
    while True:
        event, payload = _read(await anext(stream))
        if event == "snapshot":
            return payload


async def _next_decisions(stream: AsyncIterator[str | bytes | memoryview]) -> DecisionsSnapshot:
    while True:
        frame = await anext(stream)
        assert isinstance(frame, bytes)
        if frame.startswith(b"event: snapshot\n"):
            return DecisionsSnapshot.model_validate_json(frame.partition(b"data: ")[2])


async def test_egress_decision_stream_rereads_when_the_proxy_reports_a_change(
    seeded: LiveIndex, decisions: DecisionsClient, egress_admin: FakeEgressAdmin
) -> None:
    subject = (NAMESPACE, "runner-1")
    egress_admin.decisions[subject] = [decision("2026-09-02T10:00:01Z", "GET", "api.github.com", "allow")]
    drain = Drain()
    response = await live_egress_decisions(index=seeded, decisions=decisions, shutdown=drain, name="runner-1")
    stream = aiter(response.body_iterator)
    try:
        async with asyncio.timeout(10):
            first = await _next_decisions(stream)
            assert isinstance(first.decisions, list)
            assert [(d.method, d.host) for d in first.decisions] == [("GET", "api.github.com")]
            assert not first.watching

            egress_admin.decisions[subject].append(decision("2026-09-02T10:00:02Z", "POST", "pypi.org", "deny"))
            decisions.changes.notify()
            second = await _next_decisions(stream)
            assert isinstance(second.decisions, list)
            assert [d.host for d in second.decisions] == ["api.github.com", "pypi.org"]

            egress_admin.reachable = False
            decisions.changes.notify()
            assert isinstance((await _next_decisions(stream)).decisions, DecisionsUnavailable)
    finally:
        drain.begin()
        async for _ in stream:
            pass


async def test_watching_the_proxy_wakes_readers_whether_it_answers_or_not(
    decisions: DecisionsClient, egress_admin: FakeEgressAdmin
) -> None:
    woken = asyncio.Event()
    with decisions.changes.subscribe(woken):
        for reachable in (True, False):
            egress_admin.reachable = reachable
            woken.clear()
            task = asyncio.create_task(decisions.watch(retry_interval_s=0.01))
            try:
                async with asyncio.timeout(10):
                    await woken.wait()
            finally:
                task.cancel()
                await asyncio.wait([task])


async def test_egress_decision_stream_says_a_sandbox_is_missing(seeded: LiveIndex, decisions: DecisionsClient) -> None:
    drain = Drain()
    response = await live_egress_decisions(index=seeded, decisions=decisions, shutdown=drain, name="nope")
    stream = aiter(response.body_iterator)
    try:
        async with asyncio.timeout(10):
            assert isinstance((await _next_decisions(stream)).decisions, DecisionsUnavailable)
    finally:
        drain.begin()
        async for _ in stream:
            pass


if __name__ == "__main__":
    pytest_bazel.main()
