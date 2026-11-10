"""Trusted allocation and cleanup of one bounded run in the persistent test cluster."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client.models import RbacV1Subject
from pydantic import BaseModel, Field, model_validator

from agentplane.system_tests.x.shared_cluster.namespaces import TENANT_LABEL, TENANT_VALUE, Namespaces

CAPACITY_NAMESPACE = "kube-system"
CAPACITY_LEASE = "agentplane-test-capacity"
TTL_LABEL = "cleanup.kyverno.io/ttl"
OBSERVER = "test-observer"
APPLICATION = "agentplane-agent"


class CapacityUnavailableError(Exception):
    """A previous allocation still holds capacity, including pending namespace cleanup."""


class Allocation(BaseModel):
    run_id: UUID
    namespace_uids: dict[str, UUID]
    expires_at: datetime

    @model_validator(mode="after")
    def check_namespace_set(self) -> Allocation:
        if set(self.namespace_uids) != set(Namespaces(self.run_id).all):
            raise ValueError("allocation must identify exactly its own three namespaces")
        return self


class Credentials(BaseModel):
    allocation: Allocation
    application_token: str = Field(repr=False)
    observer_token: str = Field(repr=False)


async def _present(core: k8s.CoreV1Api, namespace: str) -> bool:
    try:
        await core.read_namespace(namespace)
    except ApiException as error:
        if error.status == 404:
            return False
        raise
    return True


async def _claim(api: k8s.ApiClient, run_id: UUID, now: datetime, seconds: int) -> None:
    leases = k8s.CoordinationV1Api(api)
    try:
        previous = await leases.read_namespaced_lease(CAPACITY_LEASE, CAPACITY_NAMESPACE)
    except ApiException as error:
        if error.status != 404:
            raise
    else:
        spec = previous.spec
        if (
            spec is None
            or spec.acquire_time is None
            or spec.lease_duration_seconds is None
            or spec.holder_identity is None
        ):
            raise CapacityUnavailableError("capacity lease has incomplete ownership or expiry")
        previous_names = Namespaces(UUID(spec.holder_identity))
        if spec.acquire_time + timedelta(seconds=spec.lease_duration_seconds) > now:
            raise CapacityUnavailableError(f"run {spec.holder_identity} still holds capacity")
        core = k8s.CoreV1Api(api)
        if any([await _present(core, name) for name in previous_names.all]):
            raise CapacityUnavailableError("expired run namespaces have not finished cleanup")
        # A concurrent allocator may win this CAS. Never delete its replacement lease.
        await leases.delete_namespaced_lease(
            CAPACITY_LEASE,
            CAPACITY_NAMESPACE,
            body=k8s.V1DeleteOptions(
                preconditions=k8s.V1Preconditions(
                    uid=previous.metadata.uid, resource_version=previous.metadata.resource_version
                )
            ),
        )
    try:
        await leases.create_namespaced_lease(
            CAPACITY_NAMESPACE,
            k8s.V1Lease(
                metadata=k8s.V1ObjectMeta(name=CAPACITY_LEASE),
                spec=k8s.V1LeaseSpec(holder_identity=run_id.hex, acquire_time=now, lease_duration_seconds=seconds),
            ),
        )
    except ApiException as error:
        if error.status == 409:
            raise CapacityUnavailableError("another allocator claimed capacity") from error
        raise


async def _token(
    core: k8s.CoreV1Api, namespace: str, account: str, audiences: list[str], seconds: int
) -> k8s.V1TokenRequestStatus:
    reply = await core.create_namespaced_service_account_token(
        account,
        namespace,
        k8s.AuthenticationV1TokenRequest(spec=k8s.V1TokenRequestSpec(audiences=audiences, expiration_seconds=seconds)),
    )
    if reply.status is None or not reply.status.token or reply.status.expiration_timestamp is None:
        raise RuntimeError("TokenRequest returned incomplete credentials")
    return reply.status


async def allocate(*, api: k8s.ApiClient, lifetime: timedelta, quota: dict[str, str]) -> Credentials:
    """Called by trusted setup only. Its admin ApiClient must never enter the Bazel action.

    Failed or interrupted allocation is not retried: the durable capacity lease and namespace
    TTLs bound partial effects. A subsequent allocator waits for expiry and completed cleanup.
    """
    seconds = int(lifetime.total_seconds())
    if not 600 <= seconds <= 3600:
        raise ValueError("allocation lifetime must be between 600 and 3600 seconds")
    if not quota:
        raise ValueError("an explicit resource quota is required")
    run_id = uuid4()
    names = Namespaces(run_id)
    now = datetime.now(UTC)
    expires_at = now + lifetime
    await _claim(api, run_id, now, seconds)
    core = k8s.CoreV1Api(api)
    namespace_uids = {}
    for name in names.all:
        created = await core.create_namespace(
            k8s.V1Namespace(
                metadata=k8s.V1ObjectMeta(
                    name=name,
                    labels={
                        TENANT_LABEL: TENANT_VALUE,
                        TTL_LABEL: expires_at.strftime("%Y-%m-%dT%H%M%SZ"),
                        "pod-security.kubernetes.io/enforce": "baseline",
                        "pod-security.kubernetes.io/enforce-version": "v1.34",
                        "pod-security.kubernetes.io/audit": "baseline",
                        "pod-security.kubernetes.io/audit-version": "v1.34",
                    },
                )
            )
        )
        namespace_uids[name] = UUID(created.metadata.uid)
        await core.create_namespaced_resource_quota(
            name,
            k8s.V1ResourceQuota(metadata=k8s.V1ObjectMeta(name="test-run"), spec=k8s.V1ResourceQuotaSpec(hard=quota)),
        )
    for account in (APPLICATION, OBSERVER):
        await core.create_namespaced_service_account(
            names.application,
            k8s.V1ServiceAccount(metadata=k8s.V1ObjectMeta(name=account), automount_service_account_token=False),
        )
    rbac = k8s.RbacAuthorizationV1Api(api)
    await rbac.create_namespaced_role(
        names.application,
        k8s.V1Role(
            metadata=k8s.V1ObjectMeta(name=OBSERVER),
            rules=[
                k8s.V1PolicyRule(api_groups=["agentplane.allegedly.works"], resources=["sandboxes"], verbs=["get"]),
                k8s.V1PolicyRule(api_groups=["kubevirt.io"], resources=["virtualmachineinstances"], verbs=["get"]),
                k8s.V1PolicyRule(api_groups=[""], resources=["pods/log"], verbs=["get"]),
            ],
        ),
    )
    await rbac.create_namespaced_role_binding(
        names.application,
        k8s.V1RoleBinding(
            metadata=k8s.V1ObjectMeta(name=OBSERVER),
            role_ref=k8s.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=OBSERVER),
            subjects=[RbacV1Subject(kind="ServiceAccount", name=OBSERVER, namespace=names.application)],
        ),
    )
    application = await _token(core, names.application, APPLICATION, ["agentplane"], seconds)
    observer = await _token(core, names.application, OBSERVER, [], seconds)
    return Credentials(
        allocation=Allocation(run_id=run_id, namespace_uids=namespace_uids, expires_at=expires_at),
        application_token=application.token,
        observer_token=observer.token,
    )


async def cleanup(*, api: k8s.ApiClient, allocation: Allocation) -> bool:
    """Request deletion of the exact allocated namespaces. Never strip finalizers or delete PVs.

    Capacity is released only once the namespace controller has completed every deletion.
    This operation can be called again with the same allocation after an interrupted cleanup.
    """
    core = k8s.CoreV1Api(api)
    for name, uid in allocation.namespace_uids.items():
        try:
            await core.delete_namespace(name, body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=str(uid))))
        except ApiException as error:
            if error.status != 404:
                raise
    if any([await _present(core, name) for name in allocation.namespace_uids]):
        return False
    leases = k8s.CoordinationV1Api(api)
    try:
        lease = await leases.read_namespaced_lease(CAPACITY_LEASE, CAPACITY_NAMESPACE)
    except ApiException as error:
        if error.status == 404:
            return True
        raise
    if lease.spec.holder_identity != allocation.run_id.hex:
        raise CapacityUnavailableError("capacity belongs to another allocation")
    await leases.delete_namespaced_lease(
        CAPACITY_LEASE,
        CAPACITY_NAMESPACE,
        body=k8s.V1DeleteOptions(
            preconditions=k8s.V1Preconditions(uid=lease.metadata.uid, resource_version=lease.metadata.resource_version)
        ),
    )
    return True
