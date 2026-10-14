"""Per-Sandbox scheduling leases and database fencing across Dispatcher replicas."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import update

from agentplane.proto import command_pb2, runner_pb2
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SANDBOX_UID, SandboxCluster
from agentplane.session_dispatcher.db import Dispatch, SessionOpening
from agentplane.session_dispatcher.models import DeliveryState, Reason
from agentplane.session_dispatcher.ownership import LEASE_LABEL, SandboxOwnershipLeases, _lease_name
from agentplane.session_dispatcher.store import DispatchStore
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf

WAKE_CALLER = ServiceAccountRef(namespace="agentplane", name="test-app")


async def test_one_live_lease_owner_and_expired_lease_failover(store: DispatchStore, cluster: SandboxCluster) -> None:
    sandbox_uid = UUID(SANDBOX_UID)
    old = SandboxOwnershipLeases(
        k8s_client.CoordinationV1Api(cluster.api), store, namespace=SANDBOX_NAMESPACE, holder_identity="old"
    )
    successor = SandboxOwnershipLeases(
        k8s_client.CoordinationV1Api(cluster.api), store, namespace=SANDBOX_NAMESPACE, holder_identity="successor"
    )

    old_owner, successor_owner = await asyncio.gather(
        old.acquire(SANDBOX, sandbox_uid), successor.acquire(SANDBOX, sandbox_uid)
    )
    if old_owner is None:
        old, successor = successor, old
        old_owner, successor_owner = successor_owner, old_owner
    assert old_owner is not None
    assert successor_owner is None
    assert old_owner.generation == 1
    await store.enqueue(
        session_id="session",
        sandbox_name=SANDBOX,
        sandbox_uid=sandbox_uid,
        command=command_pb2.Command(command_id="in-flight", submit_input=command_pb2.SubmitInput(text="hello")),
    )
    assert (
        await store.settle(
            "in-flight",
            owner=old_owner,
            from_states={DeliveryState.QUEUED_IN_DISPATCHER},
            to=DeliveryState.DELIVERING_TO_RUNNER,
        )
        is not None
    )

    lease_name = _lease_name(sandbox_uid)
    lease = await k8s_client.CoordinationV1Api(cluster.api).read_namespaced_lease(lease_name, SANDBOX_NAMESPACE)
    lease.spec.renew_time = datetime.now(UTC) - timedelta(seconds=60)
    await k8s_client.CoordinationV1Api(cluster.api).replace_namespaced_lease(lease_name, SANDBOX_NAMESPACE, lease)

    new_owner = await successor.acquire(SANDBOX, sandbox_uid)
    assert new_owner is not None
    assert new_owner.generation == old_owner.generation + 1
    assert new_owner.lease_transitions == old_owner.lease_transitions + 1
    assert await old.acquire(SANDBOX, sandbox_uid) is None
    assert not await old.still_owns(old_owner)

    await store.enqueue(
        session_id="session",
        sandbox_name=SANDBOX,
        sandbox_uid=sandbox_uid,
        command=command_pb2.Command(command_id="stale-wake", submit_input=command_pb2.SubmitInput(text="hello")),
        wake_caller=WAKE_CALLER,
    )
    assert not await store.note_waking("stale-wake", owner=old_owner, incarnation=None)
    assert (await store.get_dispatch("stale-wake")).wake_from_incarnation is None
    assert await store.note_waking("stale-wake", owner=new_owner, incarnation=None)
    assert await store.note_wake_requested("stale-wake", owner=new_owner) is not None
    async with store.sessions.begin() as session:
        await session.execute(
            update(Dispatch)
            .where(Dispatch.command_id == "stale-wake")
            .values(wake_requested_at=datetime.now(UTC) - timedelta(minutes=11))
        )
    failed, remaining = await store.fail_expired_wake(
        "stale-wake", owner=old_owner, reason="stale owner must not settle"
    )
    assert failed is None
    assert remaining is None
    assert (await store.get_dispatch("stale-wake")).state is DeliveryState.QUEUED_IN_DISPATCHER
    await store.open_session(
        session_id="stale-opening",
        sandbox_name=SANDBOX,
        sandbox_uid=sandbox_uid,
        spec=runner_pb2.SessionSpec(),
        setup_script=None,
        wake_caller=WAKE_CALLER,
    )
    assert not await store.note_opening_waking("stale-opening", owner=old_owner, incarnation=None)
    assert (await store.get_opening("stale-opening")).wake_from_incarnation is None
    assert await store.note_opening_waking("stale-opening", owner=new_owner, incarnation=None)
    assert await store.note_opening_wake_requested("stale-opening", owner=new_owner) is not None
    async with store.sessions.begin() as session:
        await session.execute(
            update(SessionOpening)
            .where(SessionOpening.session_id == "stale-opening")
            .values(wake_requested_at=datetime.now(UTC) - timedelta(minutes=11))
        )
    failed_opening, remaining = await store.fail_expired_opening_wake(
        "stale-opening", owner=old_owner, reason="stale owner must not settle"
    )
    assert failed_opening is None
    assert remaining is None
    assert (await store.get_opening("stale-opening")).state is DeliveryState.QUEUED_IN_DISPATCHER

    assert (
        await store.settle(
            "in-flight",
            owner=old_owner,
            from_states={DeliveryState.DELIVERING_TO_RUNNER},
            to=DeliveryState.RUNNER_ADMITTED,
        )
        is None
    )
    assert await store.recover_sandbox(new_owner)
    row = await store.get_dispatch("in-flight")
    assert (row.state, row.reason) == (DeliveryState.QUEUED_IN_DISPATCHER, Reason.DISPATCHER_RESTARTED)


async def test_stale_lease_proof_cannot_overwrite_successor_after_lease_recreation(
    store: DispatchStore, cluster: SandboxCluster
) -> None:
    sandbox_uid = UUID(SANDBOX_UID)
    api = k8s_client.CoordinationV1Api(cluster.api)
    old = SandboxOwnershipLeases(api, store, namespace=SANDBOX_NAMESPACE, holder_identity="old")
    old_owner = await old.acquire(SANDBOX, sandbox_uid)
    assert old_owner is not None
    expected_generation = await store.ownership_generation(sandbox_uid)
    # Simulate Kubernetes deleting and recreating the Lease while the old holder is paused after
    # validating its previous object. The replacement UID has no ordering relationship to the old UID.
    cluster.fake.delete("leases", _lease_name(sandbox_uid))
    cluster.fake.put(
        "leases",
        {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {
                "name": _lease_name(sandbox_uid),
                "namespace": SANDBOX_NAMESPACE,
                "uid": "recreated-lease-uid",
                "labels": {LEASE_LABEL: "true"},
                "ownerReferences": [
                    {
                        "apiVersion": SANDBOX_API_VERSION,
                        "kind": SANDBOX_KIND,
                        "name": SANDBOX,
                        "uid": SANDBOX_UID,
                        "controller": False,
                        "blockOwnerDeletion": False,
                    }
                ],
            },
            "spec": {
                "holderIdentity": "successor",
                "leaseTransitions": 1,
                "renewTime": datetime.now(UTC).isoformat(),
                "leaseDurationSeconds": 30,
            },
        },
    )
    successor = SandboxOwnershipLeases(api, store, namespace=SANDBOX_NAMESPACE, holder_identity="successor")
    new_owner = await successor.acquire(SANDBOX, sandbox_uid)
    assert new_owner is not None
    assert new_owner.generation == expected_generation + 1
    assert new_owner.lease_uid == "recreated-lease-uid"

    stale_claim = await store.claim_sandbox_owner(
        sandbox_uid,
        lease_uid=old_owner.lease_uid,
        lease_transitions=old_owner.lease_transitions,
        holder_identity=old_owner.holder_identity,
        expected_generation=expected_generation,
    )
    assert stale_claim is None


if __name__ == "__main__":
    pytest_bazel.main()
