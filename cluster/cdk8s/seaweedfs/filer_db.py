"""The SeaweedFS filer metadata DB on the SSD tier: the CNPG `Cluster` holding every filer
entry (postgres2 backend), rendered beside its hand-written credentials Secret
(`seaweedfs-filer-db-ssd-creds.sops.yaml`).

filer-db metadata is on the critical path of every SeaweedFS/git op. It was moved off the
KS-5 HDD tier to the KS-GAME NVMe tier (CNPG Case B, completed 2026-07-05) via the CNPG
standalone-replica pattern (skills/cnpg_region_switch): a streaming replica of the old
seaweedfs-filer-db was bootstrapped here (pg_basebackup), promoted, and the filer repointed
onto it. The source cluster has since been retired; the inert replica / externalClusters /
pg_basebackup stanzas were dropped once the promotion was durable.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecBootstrapInitdbSecret,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecResources,
    ClusterSpecResourcesLimits,
    ClusterSpecResourcesRequests,
    ClusterSpecStorage,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import namespace

NAME = "seaweedfs-filer-db-ssd"
OUTPUT_DIR = "cluster/k8s/seaweedfs/db"
# The application role's credentials, which the filer authenticates with too. The -creds
# name deliberately avoids CNPG's reserved <cluster>-app: CNPG auto-generates a bogus one
# (default user "app") for this cluster.
CREDENTIALS_SECRET = "seaweedfs-filer-db-ssd-creds"
_CREDENTIALS_FILE = "seaweedfs-filer-db-ssd-creds.sops.yaml"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Cluster(
        chart,
        "cluster",
        metadata=metadata(
            NAME,
            namespace.NAME,
            annotations={
                "description": (
                    "SeaweedFS filer metadata DB on the SSD tier (physically cloned from the retired"
                    " seaweedfs-filer-db, Case B git-latency migration)"
                )
            },
        ),
        spec=ClusterSpec(
            instances=2,
            # renovate: datasource=docker
            image_name="ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie",
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                tolerations=[
                    ClusterSpecAffinityTolerations(
                        key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                    )
                ],
                # Existing SSD-local replicas remain pinned by their PVs. Prefer a worker for
                # any future placement that is not constrained by an existing claim.
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh-ssd", size="2Gi"),
            # QoS / eviction protection. Without these the instance pods are BestEffort -- the
            # kubelet's first node-pressure eviction target and the OOM-killer's first victim
            # -- which is backwards for the store holding all filer metadata: every SeaweedFS
            # and git op goes through it (postgres2 backend, metadata lives here and not in the
            # filer). Every other component in this namespace is explicitly protected.
            #
            # Sized on 30d observation: the primary sits at 372Mi p50 / 410Mi max, the replica
            # at 185Mi p50. Request 512Mi is above the primary's peak so eviction ranks it
            # last; limit 1Gi leaves room for a checkpoint or autovacuum burst without being a
            # cap Postgres can trip over.
            resources=ClusterSpecResources(
                requests={
                    "cpu": ClusterSpecResourcesRequests.from_string("100m"),
                    "memory": ClusterSpecResourcesRequests.from_string("512Mi"),
                },
                limits={"memory": ClusterSpecResourcesLimits.from_string("1Gi")},
            ),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            # Application-user identity for CNPG's ongoing reconcile. NOT a re-initialization:
            # CNPG runs bootstrap exactly once, at cluster creation on empty PGDATA (this
            # cluster was actually created via pg_basebackup, see the module docstring) -- the
            # running instances are never re-init'd by this stanza. Its sole purpose is to tell
            # CNPG the application role is "seaweedfs" (this clone's owner) rather than CNPG's
            # default "app": without it, the instance-manager loops forever on `role "app"
            # does not exist` because no "app" role was cloned. CNPG keeps the seaweedfs role's
            # password in sync with this Secret -- a no-op, since the Secret holds the value
            # physically replicated from the source.
            bootstrap=ClusterSpecBootstrap(
                initdb=ClusterSpecBootstrapInitdb(
                    database="seaweedfs",
                    owner="seaweedfs",
                    secret=ClusterSpecBootstrapInitdbSecret(name=CREDENTIALS_SECRET),
                )
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml", _CREDENTIALS_FILE]),
    )


def seaweedfs_filer_db(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_namespace: Kustomization, cnpg: Kustomization
) -> Kustomization:
    name = "seaweedfs-filer-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Required to apply seaweedfs-filer-db-ssd-creds.sops.yaml (the filer DB app creds
            # CNPG syncs onto the -ssd seaweedfs role); without it Flux applies the ciphertext.
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(seaweedfs_namespace, cnpg),
        ),
    )
