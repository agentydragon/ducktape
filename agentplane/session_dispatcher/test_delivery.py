"""Commands reach a scripted runner in order, once the Sandbox is `Ready`, against real PostgreSQL and
the fake API server."""

import asyncio
from collections.abc import AsyncIterator

import grpc
import pytest
import pytest_bazel

from agentplane import sandbox_resource
from agentplane.background_task import background
from agentplane.proto import command_pb2, runner_pb2
from agentplane.sandbox_service.testing.kubernetes import SANDBOX, Cluster
from agentplane.session_dispatcher import delivery as delivery_module
from agentplane.session_dispatcher.channels import Channel
from agentplane.session_dispatcher.db import Dispatch
from agentplane.session_dispatcher.delivery import Delivery
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.models import DeliveryState, Reason
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch
from agentplane.session_dispatcher.store import DispatchStore
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.session_dispatcher.testing.scripted_runner import ScriptedRunner

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

SESSION = "test-session"


@pytest.fixture(autouse=True)
async def delivery(
    store: DispatchStore, sandboxes: SandboxWatch, runner_client: RunnerClient, ended: EndedDispatches
) -> AsyncIterator[Delivery]:
    delivery = Delivery(store, sandboxes, runner_client, ended)
    async with background(delivery.run(), name="delivery"):
        yield delivery


async def enqueue(store: DispatchStore, sandboxes: SandboxWatch, command_id: str, *, stop: bool = False) -> None:
    sandbox = sandboxes.get_sandbox(SANDBOX)
    assert sandbox is not None
    command = command_pb2.Command(command_id=command_id)
    if stop:
        command.stop_runner_session.SetInParent()
    else:
        command.submit_input.text = command_id
    await store.enqueue(session_id=SESSION, sandbox_name=SANDBOX, sandbox_uid=sandbox.metadata.uid, command=command)


async def wait_for(store: DispatchStore, command_id: str, state: DeliveryState, reason: str | None = None) -> Dispatch:
    changed = asyncio.Event()
    with store.wakeups.changes[Channel.DISPATCH].subscribe(changed):
        while True:
            changed.clear()
            row = await store.get_dispatch(command_id)
            if (row.state, row.reason) == (state, reason):
                return row
            await changed.wait()


async def test_a_session_commands_reach_a_ready_runner_in_order(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    await enqueue(store, sandboxes, "first")
    await enqueue(store, sandboxes, "second")
    for command_id in ("first", "second"):
        row = await wait_for(store, command_id, DeliveryState.RUNNER_ADMITTED)
        assert row.admission is not None
    assert runner[0].calls == [("SubmitInput", "first"), ("SubmitInput", "second")]


async def test_a_command_waits_for_the_sandbox_to_be_ready(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster
) -> None:
    set_sandbox(cluster, ready=False)
    await enqueue(store, sandboxes, "waiting")
    await wait_for(store, "waiting", DeliveryState.QUEUED_IN_DISPATCHER, Reason.SANDBOX_NOT_READY)
    set_sandbox(cluster, ready=True)
    await wait_for(store, "waiting", DeliveryState.RUNNER_ADMITTED)


async def test_a_stop_for_a_suspended_sandbox_has_nothing_to_stop(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster
) -> None:
    set_sandbox(cluster, ready=False, running=False)
    await enqueue(store, sandboxes, "stop", stop=True)
    await wait_for(store, "stop", DeliveryState.NO_OP, Reason.SANDBOX_SUSPENDED)


async def test_a_quiescing_runner_is_not_asked_again_until_the_sandbox_has_a_new_incarnation(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, runner: tuple[ScriptedRunner, str]
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


async def test_an_unreachable_runner_is_tried_again(
    store: DispatchStore,
    sandboxes: SandboxWatch,
    cluster: Cluster,
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
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.INVALID_ARGUMENT, "empty input"))
    await enqueue(store, sandboxes, "invalid")
    row = await wait_for(store, "invalid", DeliveryState.FAILED, "runner INVALID_ARGUMENT: empty input")
    assert row.admission is None


async def open_session(store: DispatchStore, sandboxes: SandboxWatch) -> None:
    sandbox = sandboxes.get_sandbox(SANDBOX)
    assert sandbox is not None
    await store.open_session(
        session_id=SESSION,
        sandbox_name=SANDBOX,
        sandbox_uid=sandbox.metadata.uid,
        spec=runner_pb2.SessionSpec(),
        setup_script=None,
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
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, runner: tuple[ScriptedRunner, str]
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
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.ALREADY_EXISTS, "created by an attempt whose reply was lost"))
    runner[0].listed_sessions.append(SESSION)
    await open_session(store, sandboxes)
    await wait_for_opening(store, DeliveryState.RUNNER_ADMITTED)


async def test_an_opening_that_conflicts_with_the_runners_session_fails(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, runner: tuple[ScriptedRunner, str]
) -> None:
    set_sandbox(cluster, ready=True)
    runner[0].failures.append((grpc.StatusCode.ALREADY_EXISTS, "another spec"))
    await open_session(store, sandboxes)
    await wait_for_opening(store, DeliveryState.FAILED, "runner ALREADY_EXISTS: another spec")


async def test_a_deleted_sandbox_ends_its_commands_for_the_streams_and_takes_its_rows(
    store: DispatchStore, sandboxes: SandboxWatch, cluster: Cluster, ended: EndedDispatches
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
