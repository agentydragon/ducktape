"""Per-Sandbox Dispatcher scheduling leases, fenced by the shared PostgreSQL database."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from kubernetes_asyncio import client as k8s_client

from agentplane.crd_group import GROUP
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.session_dispatcher.store import DispatchStore, SandboxOwner

LEASE_DURATION_S = 30
LEASE_RENEW_INTERVAL_S = 10
_CONFLICT_ATTEMPTS = 5
LEASE_LABEL = f"{GROUP}/session-dispatcher-owner"


def _micro_time(moment: datetime) -> datetime:
    """Kubernetes MicroTime requires fractional seconds in the serialized timestamp."""
    return moment.replace(microsecond=moment.microsecond or 1)


def _lease_name(sandbox_uid: UUID) -> str:
    return f"dispatcher-{sandbox_uid.hex}"


class SandboxOwnershipLeases:
    """Acquire scheduling ownership; the DB generation, not the Lease read, fences settlements."""

    def __init__(
        self,
        coordination: k8s_client.CoordinationV1Api,
        store: DispatchStore,
        *,
        namespace: str,
        holder_identity: str | None = None,
    ) -> None:
        self._coordination = coordination
        self._store = store
        self._namespace = namespace
        # A restart is a new holder even when Kubernetes reuses the same Pod name.
        self.holder_identity = holder_identity or str(uuid4())

    async def acquire(self, sandbox_name: str, sandbox_uid: UUID) -> SandboxOwner | None:
        """Renew this holder's Lease or take an expired one, then CAS its proof into PostgreSQL.

        The DB generation is deliberately captured before the fresh Lease read. On a Lease API
        conflict or DB CAS loss, the whole sequence restarts and validates the current Lease again.
        """
        name = _lease_name(sandbox_uid)
        for _ in range(_CONFLICT_ATTEMPTS):
            expected_generation = await self._store.ownership_generation(sandbox_uid)
            now = datetime.now(UTC)
            try:
                current = await self._coordination.read_namespaced_lease(name, self._namespace)
            except k8s_client.ApiException as error:
                if error.status != 404:
                    raise
                lease = self._new_lease(name, sandbox_name, sandbox_uid, now)
                try:
                    written = await self._coordination.create_namespaced_lease(self._namespace, lease)
                except k8s_client.ApiException as create_error:
                    if create_error.status == 409:
                        continue
                    raise
            else:
                if not self._owns(current, sandbox_uid):
                    raise RuntimeError(f"Dispatcher Lease {name} is owned by a different Sandbox")
                spec = current.spec
                duration = spec.lease_duration_seconds or LEASE_DURATION_S
                renew_time = spec.renew_time
                holder = spec.holder_identity
                expired = holder is None or renew_time is None or renew_time + timedelta(seconds=duration) <= now
                if holder != self.holder_identity and not expired:
                    return None
                if holder == self.holder_identity:
                    transitions = spec.lease_transitions or 1
                else:
                    transitions = (spec.lease_transitions or 0) + 1
                current.spec = k8s_client.V1LeaseSpec(
                    holder_identity=self.holder_identity,
                    lease_transitions=transitions,
                    renew_time=_micro_time(now),
                    lease_duration_seconds=LEASE_DURATION_S,
                )
                try:
                    written = await self._coordination.replace_namespaced_lease(name, self._namespace, current)
                except k8s_client.ApiException as replace_error:
                    if replace_error.status in {404, 409}:
                        continue
                    raise

            # A writer may have been superseded immediately after its successful response. Re-read
            # the Lease before the DB CAS; the expected generation makes a proof captured earlier
            # unable to overwrite a successor that commits while this caller is paused.
            try:
                fresh = await self._coordination.read_namespaced_lease(name, self._namespace)
            except k8s_client.ApiException as error:
                if error.status == 404:
                    continue
                raise
            if not self._same_acquisition(written, fresh, sandbox_uid):
                continue
            lease_uid = fresh.metadata.uid
            if lease_uid is None:
                raise RuntimeError(f"Dispatcher Lease {name} has no Kubernetes UID")
            owner = await self._store.claim_sandbox_owner(
                sandbox_uid,
                lease_uid=lease_uid,
                lease_transitions=fresh.spec.lease_transitions or 1,
                holder_identity=self.holder_identity,
                expected_generation=expected_generation,
            )
            if owner is not None:
                return owner
        return None

    async def still_owns(self, owner: SandboxOwner) -> bool:
        """Preflight external wake patches against both the live Lease and DB ownership epoch."""
        try:
            lease = await self._coordination.read_namespaced_lease(_lease_name(owner.sandbox_uid), self._namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                return False
            raise
        renew_time = lease.spec.renew_time
        duration = lease.spec.lease_duration_seconds or LEASE_DURATION_S
        return (
            lease.metadata.uid == owner.lease_uid
            and lease.spec.holder_identity == owner.holder_identity
            and lease.spec.lease_transitions == owner.lease_transitions
            and self._owns(lease, owner.sandbox_uid)
            and renew_time is not None
            and renew_time + timedelta(seconds=duration) > datetime.now(UTC)
            and await self._store.is_current_owner(owner)
        )

    def _new_lease(self, name: str, sandbox_name: str, sandbox_uid: UUID, now: datetime) -> k8s_client.V1Lease:
        return k8s_client.V1Lease(
            api_version="coordination.k8s.io/v1",
            kind="Lease",
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=self._namespace,
                labels={LEASE_LABEL: "true"},
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version=SANDBOX_API_VERSION,
                        kind=SANDBOX_KIND,
                        name=sandbox_name,
                        uid=str(sandbox_uid),
                        controller=False,
                        block_owner_deletion=False,
                    )
                ],
            ),
            spec=k8s_client.V1LeaseSpec(
                holder_identity=self.holder_identity,
                lease_transitions=1,
                renew_time=_micro_time(now),
                lease_duration_seconds=LEASE_DURATION_S,
            ),
        )

    @staticmethod
    def _owns(lease: k8s_client.V1Lease, sandbox_uid: UUID) -> bool:
        return [(ref.kind, ref.uid) for ref in lease.metadata.owner_references or []] == [
            (SANDBOX_KIND, str(sandbox_uid))
        ]

    def _same_acquisition(self, written: k8s_client.V1Lease, fresh: k8s_client.V1Lease, sandbox_uid: UUID) -> bool:
        return (
            written.metadata.uid == fresh.metadata.uid
            and written.spec.holder_identity == self.holder_identity
            and fresh.spec.holder_identity == self.holder_identity
            and written.spec.lease_transitions == fresh.spec.lease_transitions
            and self._owns(fresh, sandbox_uid)
        )
