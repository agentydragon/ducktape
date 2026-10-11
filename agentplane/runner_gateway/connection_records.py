"""Runner connection records in Kubernetes: each Sandbox's Lease, control Service and EndpointSlice.

The replica holding a Sandbox's runner connection owns its Lease (holder, epoch, liveness) and
points the Sandbox's headless control Service at its own Pod. Every replica reads Leases from a
watch cache, never from the API server per request. Contract: `agentplane/docs/runner_control.md`
§ Connection ownership and fencing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from agentplane.crd_group import GROUP
from util.agent_sandbox import SANDBOX_API, SANDBOX_KIND
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to

logger = logging.getLogger(__name__)

POD_UID_ANNOTATION = f"{GROUP}/pod-uid"
RUNNER_BOOT_ID_ANNOTATION = f"{GROUP}/runner-boot-id"
CONNECTION_EPOCH_ANNOTATION = f"{GROUP}/connection-epoch"
# Selects the Leases the cache holds; other controllers keep Leases in the Sandbox namespace too.
CONNECTION_LABEL = f"{GROUP}/runner-connection"
LEASE_DURATION_S = 30
CONTROL_PORT = "grpc"
# A selector-less Service gets no controller-managed slices; this names the slice's writer instead.
_SLICE_MANAGER = "agentplane-sandbox-service"
# Concurrent accepts and renewals on one Sandbox conflict on `resourceVersion`; each retry re-reads.
_CONFLICT_ATTEMPTS = 5


class ConnectionSupersededError(Exception):
    """A newer connection for the same Sandbox was accepted, or its Lease is gone."""


@dataclass(frozen=True)
class RecordOwner:
    """The object a Sandbox's connection records are named after and deleted with.

    Every record's name and owner reference comes from here, so pointing them at another owning
    kind is an edit to this class alone.
    """

    name: str
    uid: UUID

    @property
    def lease_name(self) -> str:
        return f"{self.name}-runner"

    @property
    def control_name(self) -> str:
        return f"{self.name}-control"

    def reference(self) -> k8s_client.V1OwnerReference:
        # For cascading deletion only; the Sandbox controller owns its Pod.
        return k8s_client.V1OwnerReference(
            api_version=SANDBOX_API.api_version,
            kind=SANDBOX_KIND,
            name=self.name,
            uid=str(self.uid),
            controller=False,
            block_owner_deletion=False,
        )

    def owns(self, record: Any) -> bool:
        """Whether a record read back names this owner and no other."""
        return [(ref.kind, ref.uid) for ref in record.metadata.owner_references or []] == [
            (SANDBOX_KIND, str(self.uid))
        ]

    @staticmethod
    def uid_of(record: Any) -> UUID:
        return UUID(one(ref for ref in record.metadata.owner_references if ref.kind == SANDBOX_KIND).uid)


def _status(error: BaseException, status: int) -> bool:
    return isinstance(error, k8s_client.ApiException) and error.status == status


def _retrying_conflicts() -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception(lambda error: _status(error, 409)),
        stop=stop_after_attempt(_CONFLICT_ATTEMPTS),
        reraise=True,
    )


def _micro_time(moment: datetime) -> datetime:
    # The client writes `isoformat()`, which drops the fraction at a whole second, and MicroTime
    # refuses a time without six fractional digits.
    return moment.replace(microsecond=moment.microsecond or 1)


@dataclass(frozen=True)
class ConnectionHolder:
    """This replica, as Leases name it and the control Service reaches it."""

    pod_name: str
    pod_ip: str
    port: int


@dataclass(frozen=True)
class ConnectionLease:
    sandbox_uid: UUID
    holder: str
    epoch: int
    renewed_at: datetime

    @classmethod
    def parse(cls, lease: k8s_client.V1Lease) -> ConnectionLease:
        return cls(
            sandbox_uid=RecordOwner.uid_of(lease),
            holder=lease.spec.holder_identity,
            epoch=lease.spec.lease_transitions,
            renewed_at=lease.spec.renew_time,
        )


class ConnectionLeases:
    """A replica's cache of runner connection Leases, by the UID of the Sandbox owning each."""

    def __init__(self) -> None:
        self._leases: dict[UUID, ConnectionLease] = {}
        self.changed = asyncio.Condition()

    def watch(self, coordination: k8s_client.CoordinationV1Api, namespace: str, *, resync_seconds: int) -> ListWatch:
        async def ignore(*_: object) -> None:
            pass

        async def notify(_: object) -> None:
            async with self.changed:
                self.changed.notify_all()

        return ListWatch(
            kinds=(
                WatchedKind(
                    name="leases",
                    list=coordination.list_namespaced_lease,
                    args=(namespace,),
                    kwargs={"label_selector": f"{CONNECTION_LABEL}=true"},
                    parse=ConnectionLease.parse,
                    key=lambda lease: lease.sandbox_uid,
                    names=lambda: set(self._leases),
                    apply=lambda key, lease: apply_to(self._leases, key, lease),
                ),
            ),
            resync_seconds=resync_seconds,
            on_change=notify,
            on_cycle=ignore,
        )

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with self.changed:
            await self.changed.wait_for(predicate)

    def read_epoch(self, sandbox_uid: UUID) -> int | None:
        """The current connection's epoch, live or not."""
        lease = self._leases.get(sandbox_uid)
        return None if lease is None else lease.epoch

    def read_live_epoch(self, sandbox_uid: UUID, *, liveness_s: float) -> int | None:
        """The current connection's epoch while it was renewed within `liveness_s`, else None."""
        lease = self._leases.get(sandbox_uid)
        if lease is None or lease.renewed_at <= datetime.now(UTC) - timedelta(seconds=liveness_s):
            return None
        return lease.epoch


class ConnectionRecords:
    """This replica's writes of the Sandbox-owned connection records."""

    def __init__(self, api: k8s_client.ApiClient, *, namespace: str, holder: ConnectionHolder) -> None:
        self._coordination = k8s_client.CoordinationV1Api(api)
        self._core = k8s_client.CoreV1Api(api)
        self._discovery = k8s_client.DiscoveryV1Api(api)
        self._namespace = namespace
        self._holder = holder

    def _metadata(self, name: str, owner: RecordOwner, **fields: Any) -> k8s_client.V1ObjectMeta:
        return k8s_client.V1ObjectMeta(
            name=name, namespace=self._namespace, owner_references=[owner.reference()], **fields
        )

    async def accept(self, owner: RecordOwner, *, pod_uid: str, runner_boot_id: str) -> int:
        """Take the Sandbox's Lease for a new connection and point its control Service here.

        Returns the new epoch; the records are written before the caller sends `Welcome`.
        """
        async for attempt in _retrying_conflicts():
            with attempt:
                try:
                    current = await self._coordination.read_namespaced_lease(owner.lease_name, self._namespace)
                except k8s_client.ApiException as error:
                    if error.status != 404:
                        raise
                    current = None
                epoch = 1 if current is None else current.spec.lease_transitions + 1
                lease = k8s_client.V1Lease(
                    api_version="coordination.k8s.io/v1",
                    kind="Lease",
                    # A Lease left by an earlier incarnation of the name moves to this one.
                    metadata=self._metadata(
                        owner.lease_name,
                        owner,
                        labels={CONNECTION_LABEL: "true"},
                        annotations={POD_UID_ANNOTATION: pod_uid, RUNNER_BOOT_ID_ANNOTATION: runner_boot_id},
                        resource_version=None if current is None else current.metadata.resource_version,
                    ),
                    spec=k8s_client.V1LeaseSpec(
                        holder_identity=self._holder.pod_name,
                        lease_transitions=epoch,
                        renew_time=_micro_time(datetime.now(UTC)),
                        lease_duration_seconds=LEASE_DURATION_S,
                    ),
                )
                if current is None:
                    await self._coordination.create_namespaced_lease(self._namespace, lease)
                else:
                    await self._coordination.replace_namespaced_lease(owner.lease_name, self._namespace, lease)
        await self._ensure_control_service(owner)
        await self._point_control_endpoints(owner, epoch=epoch)
        return epoch

    async def renew(self, owner: RecordOwner, *, epoch: int, seen_at: datetime) -> None:
        """Record liveness for the connection holding `epoch`, or learn that it no longer does."""
        async for attempt in _retrying_conflicts():
            with attempt:
                try:
                    lease = await self._coordination.read_namespaced_lease(owner.lease_name, self._namespace)
                except k8s_client.ApiException as error:
                    if error.status == 404:
                        raise ConnectionSupersededError(owner.name) from error
                    raise
                if lease.spec.holder_identity != self._holder.pod_name or lease.spec.lease_transitions != epoch:
                    raise ConnectionSupersededError(owner.name)
                lease.spec.renew_time = _micro_time(seen_at)
                await self._coordination.replace_namespaced_lease(owner.lease_name, self._namespace, lease)

    async def _ensure_control_service(self, owner: RecordOwner) -> None:
        service = k8s_client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=self._metadata(owner.control_name, owner),
            # Headless and selector-less: DNS returns the EndpointSlice's address, with no kube-proxy rules.
            spec=k8s_client.V1ServiceSpec(
                cluster_ip="None", ports=[k8s_client.V1ServicePort(name=CONTROL_PORT, port=self._holder.port)]
            ),
        )
        async for attempt in _retrying_conflicts():
            with attempt:
                try:
                    current = await self._core.read_namespaced_service(owner.control_name, self._namespace)
                except k8s_client.ApiException as error:
                    if error.status != 404:
                        raise
                    await self._core.create_namespaced_service(self._namespace, service)
                    return
                if owner.owns(current):
                    return
                # An earlier incarnation's, not yet collected; its deletion would take this one's too.
                current.metadata.owner_references = [owner.reference()]
                await self._core.replace_namespaced_service(owner.control_name, self._namespace, current)

    async def _point_control_endpoints(self, owner: RecordOwner, *, epoch: int) -> None:
        async for attempt in _retrying_conflicts():
            with attempt:
                try:
                    current = await self._discovery.read_namespaced_endpoint_slice(owner.control_name, self._namespace)
                except k8s_client.ApiException as error:
                    if error.status != 404:
                        raise
                    current = None
                if (
                    current is not None
                    and owner.owns(current)
                    and int(current.metadata.annotations[CONNECTION_EPOCH_ANNOTATION]) >= epoch
                ):
                    logger.info(
                        "Kept %s: it already names a connection at least as new as %s", owner.control_name, epoch
                    )
                    return
                endpoints = k8s_client.V1EndpointSlice(
                    api_version="discovery.k8s.io/v1",
                    kind="EndpointSlice",
                    metadata=self._metadata(
                        owner.control_name,
                        owner,
                        labels={
                            "kubernetes.io/service-name": owner.control_name,
                            "endpointslice.kubernetes.io/managed-by": _SLICE_MANAGER,
                        },
                        annotations={CONNECTION_EPOCH_ANNOTATION: str(epoch)},
                        resource_version=None if current is None else current.metadata.resource_version,
                    ),
                    address_type="IPv4",
                    endpoints=[
                        k8s_client.V1Endpoint(
                            addresses=[self._holder.pod_ip], conditions=k8s_client.V1EndpointConditions(ready=True)
                        )
                    ],
                    ports=[
                        k8s_client.DiscoveryV1EndpointPort(name=CONTROL_PORT, port=self._holder.port, protocol="TCP")
                    ],
                )
                if current is None:
                    await self._discovery.create_namespaced_endpoint_slice(self._namespace, endpoints)
                else:
                    await self._discovery.replace_namespaced_endpoint_slice(
                        owner.control_name, self._namespace, endpoints
                    )
