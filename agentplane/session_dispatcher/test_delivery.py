"""Commands reach a scripted runner in order, once the Sandbox is `Ready`, against real PostgreSQL and
the fake API server."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiException
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane import sandbox_resource
from agentplane.background_task import background
from agentplane.proto import command_pb2, runner_pb2
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster
from agentplane.session_dispatcher import delivery as delivery_module
from agentplane.session_dispatcher.channels import Channel
from agentplane.session_dispatcher.db import Dispatch
from agentplane.session_dispatcher.delivery import Delivery
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.models import DeliveryState, Reason
from agentplane.session_dispatcher.ownership import SandboxOwnershipLeases
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch
from agentplane.session_dispatcher.store import DispatchStore
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.session_dispatcher.testing.scripted_runner import ScriptedRunner
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

SESSION = "test-session"


@pytest.fixture(autouse=True)
async def delivery(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    runner_client: RunnerClient,
    ended: EndedDispatches,
    cluster: SandboxCluster,
) -> AsyncIterator[Delivery]:
    ownership = SandboxOwnershipLeases(
        k8s_client.CoordinationV1Api(cluster.api), store, namespace=SANDBOX_NAMESPACE, holder_identity="test-dispatcher"
    )
    delivery = Delivery(store, sandboxes, runner_client, ended, cluster.inventory, ownership)
    async with background(delivery.run(), name="delivery"):
        yield delivery


async def enqueue(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    command_id: str,
    *,
    session_id: str = SESSION,
    stop: bool = False,
    wake_eligible: bool = False,
) -> None:
    sandbox = sandboxes.get_sandbox(SANDBOX)
    assert sandbox is not None
    command = command_pb2.Command(command_id=command_id)
    if stop:
        command.stop_runner_session.SetInParent()
    else:
        command.submit_input.text = command_id
    await store.enqueue(
        session_id=session_id,
        sandbox_name=SANDBOX,
        sandbox_uid=sandbox.metadata.uid,
        command=command,
        wake_eligible=wake_eligible,
    )


async def wait_for(store: DispatchStore, command_id: str, state: DeliveryState, reason: str | None = None) -> Dispatch:
    changed = asyncio.Event()
    with store.wakeups.changes[Channel.DISPATCH].subscribe(changed):
        while True:
            changed.clear()
            row = await store.get_dispatch(command_id)
            if (row.state, row.reason) == (state, reason):
                return row
            await changed.wait()


async def wait_for_sandbox(sandboxes: SandboxWatch, predicate: Callable[[sandbox_resource.Sandbox], bool]) -> None:
    changed = asyncio.Event()
    with sandboxes.changed.subscribe(changed):
        while True:
            changed.clear()
            sandbox = sandboxes.get_sandbox(SANDBOX)
            if sandbox is not None and predicate(sandbox):
                return
            await changed.wait()


async def wait_for_waking(store: DispatchStore, command_id: str) -> Dispatch:
    changed = asyncio.Event()
    with store.wakeups.changes[Channel.DISPATCH].subscribe(changed):
        while True:
            changed.clear()
            row = await store.get_dispatch(command_id)
            if row.wake_from_incarnation is not None:
                return row
            await changed.wait()


async def wait_for_opening_waking(store: DispatchStore) -> None:
    changed = asyncio.Event()
    with store.wakeups.changes[Channel.DISPATCH].subscribe(changed):
        while True:
            changed.clear()
            opening = await store.get_opening(SESSION)
            if opening.wake_from_incarnation is not None:
                return
            await changed.wait()


async def wait_for_delivery_state(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    runner: ScriptedRunner,
    command_id: str,
    state: DeliveryState,
    reason: str | None = None,
) -> Dispatch:
    try:
        return await asyncio.wait_for(wait_for(store, command_id, state, reason), timeout=10)
    except TimeoutError:
        row = await store.get_dispatch(command_id)
        sandbox = sandboxes.get_sandbox(row.sandbox_name)
        observed_sandbox = (
            None
            if sandbox is None
            else {
                "mode": sandbox.operating_mode,
                "ready": sandbox.ready,
                "incarnation": None if sandbox.status.incarnation is None else sandbox.status.incarnation.uid,
                "wake_on_send": sandbox.spec.wake_on_send,
            }
        )
        raise AssertionError(
            f"{command_id} waited for {(state, reason)}, got {(row.state, row.reason)}; "
            f"wake_from_incarnation={row.wake_from_incarnation}, sandbox={observed_sandbox}, runner={runner.calls}"
        ) from None


async def test_a_session_commands_reach_a_ready_runner_in_order(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    await enqueue(store, sandboxes, "first")
    await enqueue(store, sandboxes, "second")
    for command_id in ("first", "second"):
        row = await wait_for(store, command_id, DeliveryState.RUNNER_ADMITTED)
        assert row.admission is not None
    assert runner[0].calls == [("SubmitInput", "first"), ("SubmitInput", "second")]


async def test_a_peer_replica_insert_wakes_the_sandbox_owner(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    cluster: SandboxCluster,
    runner: tuple[ScriptedRunner, str],
    engine: AsyncEngine,
) -> None:
    set_sandbox(cluster, ready=True)
    peer = DispatchStore(engine, queue_cap=100)
    sandbox = sandboxes.get_sandbox(SANDBOX)
    assert sandbox is not None
    async with peer.wakeups.listener.listen():
        await peer.enqueue(
            session_id=SESSION,
            sandbox_name=SANDBOX,
            sandbox_uid=sandbox.metadata.uid,
            command=command_pb2.Command(command_id="peer-insert", submit_input=command_pb2.SubmitInput(text="peer")),
        )
        await wait_for(store, "peer-insert", DeliveryState.RUNNER_ADMITTED)
    assert runner[0].calls == [("SubmitInput", "peer-insert")]


async def test_a_wake_retry_rechecks_ownership_after_failover(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    cluster: SandboxCluster,
    delivery: Delivery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await delivery._sync_ownership()
    sandbox = sandboxes.get_sandbox(SANDBOX)
    assert sandbox is not None
    stale_owner = delivery._owners[sandbox.metadata.uid]
    coordination = k8s_client.CoordinationV1Api(cluster.api)

    calls: list[str] = []

    async def wake(name: str, *, uid: str) -> bool:
        calls.append(f"{name}:{uid}")
        raise ApiException(status=500)

    monkeypatch.setattr(cluster.inventory, "wake_for_send", wake)

    async def notified(reason: Reason) -> None:
        del reason

    await delivery._request_sandbox_wake(
        key="stale-wake",
        sandbox_name=sandbox.metadata.name,
        sandbox_uid=sandbox.metadata.uid,
        owner=stale_owner,
        notify=notified,
    )
    assert calls == [f"{sandbox.metadata.name}:{sandbox.metadata.uid}"]

    lease = await coordination.read_namespaced_lease(
        f"dispatcher-{sandbox.metadata.uid.hex}", SANDBOX_NAMESPACE
    )
    lease.spec.renew_time = datetime.now(UTC) - timedelta(seconds=60)
    await coordination.replace_namespaced_lease(lease.metadata.name, SANDBOX_NAMESPACE, lease)
    successor = SandboxOwnershipLeases(
        coordination, store, namespace=SANDBOX_NAMESPACE, holder_identity="successor"
    )
    assert await successor.acquire(SANDBOX, sandbox.metadata.uid) is not None

    # A later retry from the old task revalidates the Lease and stops after takeover.
    await delivery._request_sandbox_wake(
        key="stale-wake",
        sandbox_name=sandbox.metadata.name,
        sandbox_uid=sandbox.metadata.uid,
        owner=stale_owner,
        notify=notified,
    )
    assert calls == [f"{sandbox.metadata.name}:{sandbox.metadata.uid}"]


async def test_a_command_waits_for_the_sandbox_to_be_ready(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster
) -> None:
    set_sandbox(cluster, ready=False)
    await enqueue(store, sandboxes, "waiting")
    await wait_for(store, "waiting", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_NOT_READY)
    set_sandbox(cluster, ready=True)
    await wait_for(store, "waiting", DeliveryState.RUNNER_ADMITTED)


async def test_a_stop_for_a_suspended_sandbox_has_nothing_to_stop(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster
) -> None:
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await enqueue(store, sandboxes, "stop", stop=True, wake_eligible=True)
    await wait_for(store, "stop", DeliveryState.NO_OP, Reason.SANDBOX_SUSPENDED)
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["running"] is False


async def test_an_eligible_send_wakes_once_and_waits_for_a_new_ready_incarnation(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await enqueue(store, sandboxes, "wake", wake_eligible=True)
    await wait_for(store, "wake", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_RESUMING)

    patched = cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]
    assert patched["spec"]["running"] is True
    assert patched["spec"]["wakeGeneration"] == 1
    assert (await store.get_dispatch("wake")).wake_from_incarnation == "pod-1"
    assert runner[0].calls == []

    set_sandbox(cluster, ready=True, incarnation="pod-1", wake_on_send=True)
    await wait_for_sandbox(
        sandboxes,
        lambda sandbox: (
            sandbox.ready and sandbox.status.incarnation is not None and sandbox.status.incarnation.uid == "pod-1"
        ),
    )
    current = sandboxes.get_sandbox(SANDBOX)
    assert current is not None
    row = await store.get_dispatch("wake")
    assert delivery_module._awaiting_new_incarnation(current, row.wake_from_incarnation)
    assert runner[0].calls == []

    set_sandbox(cluster, ready=True, incarnation="pod-2", wake_on_send=True)
    await wait_for(store, "wake", DeliveryState.RUNNER_ADMITTED)
    assert runner[0].calls == [("SubmitInput", "wake")]


async def test_concurrent_queued_sends_share_one_sandbox_resume(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await wait_for_sandbox(sandboxes, lambda sandbox: sandbox.suspended and sandbox.spec.wake_on_send)
    await enqueue(store, sandboxes, "storm-one", session_id="session-one", wake_eligible=True)
    await enqueue(store, sandboxes, "storm-two", session_id="session-two", wake_eligible=True)
    for command_id in ("storm-one", "storm-two"):
        await wait_for_delivery_state(
            store, sandboxes, runner[0], command_id, DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_RESUMING
        )

    patched = cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]
    assert patched["spec"]["running"] is True
    assert patched["spec"]["wakeGeneration"] == 1
    assert (await store.get_dispatch("storm-one")).wake_from_incarnation == "pod-1"
    assert (await store.get_dispatch("storm-two")).wake_from_incarnation == "pod-1"

    set_sandbox(cluster, ready=True, incarnation="pod-2", wake_on_send=True)
    await wait_for_sandbox(
        sandboxes,
        lambda sandbox: (
            sandbox.ready and sandbox.status.incarnation is not None and sandbox.status.incarnation.uid == "pod-2"
        ),
    )
    for command_id in ("storm-one", "storm-two"):
        await wait_for_delivery_state(store, sandboxes, runner[0], command_id, DeliveryState.RUNNER_ADMITTED)
    assert set(runner[0].calls) == {("SubmitInput", "storm-one"), ("SubmitInput", "storm-two")}


async def test_an_eligible_open_session_wakes_and_waits_for_a_new_incarnation(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await open_session(store, sandboxes, wake_eligible=True)
    await wait_for_opening(store, DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_RESUMING)

    patched = cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]
    assert patched["spec"]["running"] is True
    assert patched["spec"]["wakeGeneration"] == 1
    assert (await store.get_opening(SESSION)).wake_from_incarnation == "pod-1"
    assert runner[0].calls == []

    set_sandbox(cluster, ready=True, incarnation="pod-2", wake_on_send=True)
    await wait_for_opening(store, DeliveryState.RUNNER_ADMITTED)
    assert runner[0].calls == [("OpenSession", SESSION)]


@pytest.mark.parametrize("opening", [False, True], ids=["command", "opening"])
async def test_a_wake_patch_is_retried_after_a_transient_api_failure(
    opening: bool,
    store: DispatchStore,
    sandboxes: SandboxWatch,
    cluster: SandboxCluster,
    runner: tuple[ScriptedRunner, str],
    delivery: Delivery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "RETRY_INITIAL_S", 0.01)
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    custom = cluster.inventory._custom_objects
    original_patch = custom.patch_namespaced_custom_object
    attempts = 0

    async def fail_once(
        group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApiException(status=500)
        return await original_patch(group, version, namespace, plural, name, body, _content_type=_content_type)

    monkeypatch.setattr(custom, "patch_namespaced_custom_object", fail_once)
    key = delivery_module._opening_key(SESSION) if opening else "wake-retry"
    if opening:
        await open_session(store, sandboxes, wake_eligible=True)
        await wait_for_opening(store, DeliveryState.QUEUED_IN_DISPATCHER, Reason.DELIVERY_UNAVAILABLE)
        await wait_for_opening(store, DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_RESUMING)
    else:
        await enqueue(store, sandboxes, "wake-retry", wake_eligible=True)
        await wait_for(store, "wake-retry", DeliveryState.QUEUED_IN_DISPATCHER, Reason.DELIVERY_UNAVAILABLE)
        await wait_for(store, "wake-retry", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_RESUMING)

    assert attempts == 2
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["wakeGeneration"] == 1
    assert key not in delivery._retry_at
    assert delivery._next_retry_in() is None


async def test_canceling_a_send_clears_its_wake_retry_deadline(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    cluster: SandboxCluster,
    runner: tuple[ScriptedRunner, str],
    delivery: Delivery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "RETRY_INITIAL_S", 30)
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    custom = cluster.inventory._custom_objects

    async def fail_patch(
        group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        del group, version, namespace, plural, name, body, _content_type
        raise ApiException(status=500)

    monkeypatch.setattr(custom, "patch_namespaced_custom_object", fail_patch)
    await enqueue(store, sandboxes, "cancel-wake-retry", wake_eligible=True)
    await wait_for_delivery_state(
        store,
        sandboxes,
        runner[0],
        "cancel-wake-retry",
        DeliveryState.QUEUED_IN_DISPATCHER,
        Reason.DELIVERY_UNAVAILABLE,
    )
    assert delivery._retry_at["cancel-wake-retry"] > asyncio.get_running_loop().time()

    await store.cancel("cancel-wake-retry")
    async with asyncio.TaskGroup() as tasks:
        await delivery._start(tasks)

    assert "cancel-wake-retry" not in delivery._retry_at
    assert "cancel-wake-retry" not in delivery._attempts
    assert delivery._next_retry_in() is None


@pytest.mark.parametrize("opening", [False, True], ids=["command", "opening"])
async def test_a_wake_marker_survives_dispatcher_restart_before_the_patch(
    opening: bool,
    store: DispatchStore,
    sandboxes: SandboxWatch,
    runner_client: RunnerClient,
    ended: EndedDispatches,
    cluster: SandboxCluster,
    delivery: Delivery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    original_wake = cluster.inventory.wake_for_send
    interrupted = asyncio.Event()

    async def stop_after_marker(name: str, *, uid: str) -> bool:
        del name, uid
        interrupted.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(cluster.inventory, "wake_for_send", stop_after_marker)
    if opening:
        await open_session(store, sandboxes, wake_eligible=True)
        await wait_for_opening_waking(store)
    else:
        await enqueue(store, sandboxes, "wake-restart", wake_eligible=True)
        await wait_for_waking(store, "wake-restart")
    await interrupted.wait()
    await asyncio.sleep(0)

    monkeypatch.setattr(cluster.inventory, "wake_for_send", original_wake)
    restarted = Delivery(store, sandboxes, runner_client, ended, cluster.inventory, delivery.ownership)
    await restarted._sync_ownership()
    async with asyncio.TaskGroup() as tasks:
        if opening:
            await restarted._start_openings(tasks)
            assert (await store.get_opening(SESSION)).reason == Reason.SANDBOX_RESUMING
        else:
            await restarted._start(tasks)
            assert (await store.get_dispatch("wake-restart")).reason == Reason.SANDBOX_RESUMING

    patched = cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]
    assert patched["spec"]["running"] is True
    assert patched["spec"]["wakeGeneration"] == 1


async def test_a_send_does_not_wake_when_the_gate_or_authenticated_eligibility_is_off(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, delivery: Delivery
) -> None:
    set_sandbox(cluster, ready=False, running=False)
    delivery._delay_retry("gate-off")
    await enqueue(store, sandboxes, "gate-off", wake_eligible=True)
    await wait_for(store, "gate-off", DeliveryState.QUEUED_IN_DISPATCHER, Reason.WAKE_ON_SEND_DISABLED)
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["running"] is False
    assert "gate-off" not in delivery._retry_at
    assert "gate-off" not in delivery._attempts
    await store.cancel("gate-off")

    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await enqueue(store, sandboxes, "caller-not-eligible")
    await wait_for(store, "caller-not-eligible", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_SUSPENDED)
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["running"] is False


async def test_deletion_beats_an_eligible_send_wake(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, delivery: Delivery
) -> None:
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True, deleting=True)
    delivery._delay_retry("deleting")
    await enqueue(store, sandboxes, "deleting", wake_eligible=True)
    await wait_for(store, "deleting", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_DELETED)
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["running"] is False
    assert "deleting" not in delivery._retry_at
    assert "deleting" not in delivery._attempts


async def test_a_quiescing_runner_is_not_asked_again_until_the_sandbox_has_a_new_incarnation(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.ABORTED, "quiescing"))
    await enqueue(store, sandboxes, "refused")
    await wait_for(store, "refused", DeliveryState.QUEUED_IN_DISPATCHER, Reason.RUNNER_REFUSED_WORK)
    # A change that is not a new incarnation delivers nothing.
    set_sandbox(cluster, ready=True)
    await wait_for(store, "refused", DeliveryState.QUEUED_IN_DISPATCHER, Reason.RUNNER_REFUSED_WORK)
    assert len(runner[0].calls) == 1
    set_sandbox(cluster, ready=True, incarnation="pod-2")
    await wait_for(store, "refused", DeliveryState.RUNNER_ADMITTED)


async def test_a_message_aborted_while_sealing_is_redelivered_after_the_send_wake(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.ABORTED, "sealing"))
    await enqueue(store, sandboxes, "seal-and-wake", wake_eligible=True)
    await wait_for(store, "seal-and-wake", DeliveryState.QUEUED_IN_DISPATCHER, Reason.RUNNER_REFUSED_WORK)

    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await wait_for(store, "seal-and-wake", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_RESUMING)
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["wakeGeneration"] == 1

    set_sandbox(cluster, ready=True, incarnation="pod-2", wake_on_send=True)
    await wait_for(store, "seal-and-wake", DeliveryState.RUNNER_ADMITTED)
    assert [call for call, _ in runner[0].calls] == ["SubmitInput", "SubmitInput"]


async def test_an_unreachable_runner_is_tried_again(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    cluster: SandboxCluster,
    runner: tuple[ScriptedRunner, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "RETRY_INITIAL_S", 0.01)
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.UNAVAILABLE, "not the replica holding the runner"))
    await enqueue(store, sandboxes, "retried")
    await wait_for(store, "retried", DeliveryState.RUNNER_ADMITTED)
    assert [call for call, _ in runner[0].calls] == ["SubmitInput", "SubmitInput"]


async def test_a_command_the_runner_refuses_fails_with_its_status(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.INVALID_ARGUMENT, "empty input"))
    await enqueue(store, sandboxes, "invalid")
    row = await wait_for(store, "invalid", DeliveryState.FAILED, "runner INVALID_ARGUMENT: empty input")
    assert row.admission is None


async def open_session(store: DispatchStore, sandboxes: SandboxWatch, *, wake_eligible: bool = False) -> None:
    sandbox = sandboxes.get_sandbox(SANDBOX)
    assert sandbox is not None
    await store.open_session(
        session_id=SESSION,
        sandbox_name=SANDBOX,
        sandbox_uid=sandbox.metadata.uid,
        spec=runner_pb2.SessionSpec(),
        setup_script=None,
        wake_eligible=wake_eligible,
    )


async def wait_for_opening(store: DispatchStore, state: DeliveryState, reason: str | None = None) -> None:
    changed = asyncio.Event()
    with store.wakeups.changes[Channel.DISPATCH].subscribe(changed):
        while True:
            changed.clear()
            opening = await store.get_opening(SESSION)
            if (opening.state, opening.reason) == (state, reason):
                return
            await changed.wait()


async def test_a_session_is_opened_before_its_commands_once_the_sandbox_is_ready(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=False)
    await open_session(store, sandboxes)
    await enqueue(store, sandboxes, "first")
    await wait_for_opening(store, DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_NOT_READY)
    set_sandbox(cluster, ready=True)
    await wait_for(store, "first", DeliveryState.RUNNER_ADMITTED)
    assert runner[0].calls == [("OpenSession", SESSION), ("SubmitInput", "first")]
    assert (await store.get_opening(SESSION)).attached is not None


async def test_an_opening_the_runner_already_holds_is_opened_once_it_lists_the_session(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.ALREADY_EXISTS, "created by an attempt whose reply was lost"))
    runner[0].listed_sessions.append(SESSION)
    await open_session(store, sandboxes)
    await wait_for_opening(store, DeliveryState.RUNNER_ADMITTED)


async def test_an_opening_that_conflicts_with_the_runners_session_fails(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.ALREADY_EXISTS, "another spec"))
    await open_session(store, sandboxes)
    await wait_for_opening(store, DeliveryState.FAILED, "runner ALREADY_EXISTS: another spec")


async def test_a_deleted_sandbox_ends_its_commands_for_the_streams_and_takes_its_rows(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: SandboxCluster, ended: EndedDispatches
) -> None:
    set_sandbox(cluster, ready=False)
    await enqueue(store, sandboxes, "orphaned")
    await wait_for(store, "orphaned", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_NOT_READY)
    published = asyncio.Event()
    with ended.subscribe(published) as ends:
        cluster.fake.delete(sandbox_resource.SANDBOXES_PLURAL, SANDBOX)
        await published.wait()
    assert [(row.command_id, row.state, row.reason) for row in ends] == [
        ("orphaned", DeliveryState.FAILED, Reason.SANDBOX_DELETED)
    ]
    assert not await store.list_dispatches(session_id=SESSION)


if __name__ == "__main__":
    pytest_bazel.main()
