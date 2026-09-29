"""CNPG `Cluster`s: the shape every generated Postgres shares, its placement, and the reference
its clients address it by."""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import ApiObjectMetadata
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecBootstrapInitdbSecret,
    ClusterSpecManaged,
    ClusterSpecMonitoring,
    ClusterSpecPlugins,
    ClusterSpecPostgresql,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecResources,
    ClusterSpecStorage,
)
from constructs import Construct

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.local_path_provisioner import SSD_STORAGE_CLASSES
from cluster.cdk8s.node_scheduling import Placement
from cluster.cdk8s.providers.cnpg.cluster import Cluster
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

# renovate: datasource=docker
POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie"
# The CNPG-I plugin that archives a Cluster's WAL and takes its base backups into an ObjectStore.
BARMAN_PLUGIN = "barman-cloud.cloudnative-pg.io"
# The port of every Service CNPG creates for a Cluster.
PORT = Port(name="postgres", number=5432)

_CONTROL_PLANE_TOLERATION = ClusterSpecAffinityTolerations(
    key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule"
)


@dataclass(frozen=True)
class PostgresRef:
    """A CNPG Cluster as clients address it: CNPG names the primary's Service `<name>-rw` and labels
    every instance Pod `cnpg.io/cluster: <name>`. `app_secret` holds the credentials of the owner
    role `bootstrap.initdb` names."""

    name: str
    namespace: str
    app_secret: SecretRef

    @classmethod
    def generated(cls, *, name: str, namespace: str) -> PostgresRef:
        """A Cluster whose owner credentials CNPG generates into `<name>-app`."""
        return cls(name=name, namespace=namespace, app_secret=SecretRef(namespace=namespace, name=f"{name}-app"))

    @property
    def rw(self) -> ServiceRef:
        """The primary's Service."""
        return ServiceRef(
            name=f"{self.name}-rw",
            port=PORT,
            pods=Pods(namespace=self.namespace, labels=(("cnpg.io/cluster", self.name),)),
        )


def _affinity(*, placement: Placement, storage_class: str) -> ClusterSpecAffinity:
    """One instance per node within `placement`, required: instances sharing a node share its
    failure, and preferred anti-affinity lets the scheduler co-locate them. A Cluster tolerates
    control-plane nodes exactly when its storage is SSD, since OVH's SSD nodes are its control
    planes."""
    return ClusterSpecAffinity(
        node_selector=placement.node_selector,
        tolerations=[_CONTROL_PLANE_TOLERATION] if storage_class in SSD_STORAGE_CLASSES else None,
        topology_key="kubernetes.io/hostname",
        pod_anti_affinity_type="required",
    )


def same_owner_initdb(
    name: str, *, secret: SecretRef | None = None, locale_c_type: str | None = None, locale_collate: str | None = None
) -> ClusterSpecBootstrapInitdb:
    """`ClusterSpecBootstrapInitdb` for the common case where the app's database and its
    owning role both take the app's own name. `secret` holds the owner's credentials where they
    are not the `<cluster>-app` Secret CNPG generates."""
    return ClusterSpecBootstrapInitdb(
        database=name,
        owner=name,
        secret=None if secret is None else ClusterSpecBootstrapInitdbSecret(name=secret.name),
        locale_c_type=locale_c_type,
        locale_collate=locale_collate,
    )


def cluster(
    scope: Construct,
    id: str,
    *,
    ref: PostgresRef,
    placement: Placement,
    storage_class: str,
    size: str,
    initdb: ClusterSpecBootstrapInitdb | None,
    wal_archive: bool,
    instances: int = 2,
    image_name: str | None = POSTGRES_IMAGE,
    annotations: dict[str, str] | None = None,
    managed: ClusterSpecManaged | None = None,
    postgresql: ClusterSpecPostgresql | None = None,
    resources: ClusterSpecResources | None = None,
) -> Cluster:
    """`ref`'s CNPG `Cluster`. Keywords are `ClusterSpec` fields under the same names and types,
    except `initdb` (`bootstrap.initdb`; `None` leaves CNPG's default, an `app` database and
    owner), `storage_class`/`size` (`storage`), `placement` (the zone or region pin), from which
    `_affinity` derives placement, and `wal_archive`, which makes the barman-cloud plugin archive
    WAL to the ObjectStore named `ref.name`. Our policy: 2 instances
    (cluster/docs/cnpg_conventions.md R2) on `POSTGRES_IMAGE`; `image_name=None` runs the
    operator's default image. Every Cluster disables the liveness isolation check -- CNPG 1.27+
    otherwise kills a primary it considers isolated on a transient network blip -- and enables
    the PodMonitor.
    """
    return Cluster(
        scope,
        id,
        metadata=ApiObjectMetadata(name=ref.name, namespace=ref.namespace, annotations=annotations),
        storage=ClusterSpecStorage(storage_class=storage_class, size=size),
        instances=instances,
        image_name=image_name,
        bootstrap=None if initdb is None else ClusterSpecBootstrap(initdb=initdb),
        affinity=_affinity(placement=placement, storage_class=storage_class),
        managed=managed,
        postgresql=postgresql,
        plugins=(
            [ClusterSpecPlugins(name=BARMAN_PLUGIN, is_wal_archiver=True, parameters={"barmanObjectName": ref.name})]
            if wal_archive
            else None
        ),
        resources=resources,
        probes=ClusterSpecProbes(
            liveness=ClusterSpecProbesLiveness(isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False))
        ),
        # TODO: Migrate to manually managed PodMonitors (enablePodMonitor is deprecated).
        monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
    )
