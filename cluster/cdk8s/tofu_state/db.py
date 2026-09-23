"""The CNPG Cluster holding every tofu-controller Terraform state, and the `tofu-state-db`
Kustomization that owns it together with the tofu-state Namespace.

Hand-written beside the generated output: `db/credentials.sops.yaml`, the `tfstate` role
password. CNPG reconciles the role from that Secret through `spec.managed.roles`, so a
SOPS rotation propagates to Postgres as `ALTER ROLE`; Terraform runners (`PGPASSWORD`)
and `cluster/.envrc` read the same Secret directly.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecMonitoring,
    ClusterSpecPostgresql,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.agentplane import node_scheduling
from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import CNPG_DATABASE_READY, write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/tofu-state"
_DB_DIR = f"{OUTPUT_DIR}/db"
_NAMESPACE = "tofu-state"
_CLUSTER_NAME = "tofu-state-db-ovh"
_CREDENTIALS_FILE = "credentials.sops.yaml"


def chart(app: App) -> Chart:
    chart = Chart(app, _CLUSTER_NAME, disable_resource_name_hashes=True)
    Cluster(
        chart,
        "cluster",
        metadata=metadata(_CLUSTER_NAME, _NAMESPACE),
        spec=ClusterSpec(
            instances=2,
            image_name="ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie",
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            # A tf-runner holds a session-scoped advisory lock for the length of a plan or
            # apply. When its node drops off the network the session survives -- no RST from
            # a vanished peer -- and every later run on that state fails with "error
            # acquiring the state lock" until the kernel reaps the socket. On the defaults
            # (tcp_keepalive_time 7200 + 9 probes x 75s) that is 2h11m, and tofu-controller
            # retries into the lock the whole time: a single node loss wedged sso-providers,
            # dns-records, agent-machine-access and alloy-otlp-bearer-token on 2026-09-16,
            # and through sso-providers-tf -> forgejo -> forgejo-images, 55 Kustomizations
            # behind them.
            #
            # Probing an idle connection detects a dead peer in ~2min instead. A live runner
            # answers the probes, so a long plan is never cut off; only a peer that is
            # actually gone is. idle_session_timeout is the wrong knob here -- it would kill
            # a healthy runner that is idle between statements mid-apply.
            postgresql=ClusterSpecPostgresql(
                parameters={"tcp_keepalives_idle": "60", "tcp_keepalives_interval": "10", "tcp_keepalives_count": "6"}
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/zone": node_scheduling.ZONE},
                tolerations=[
                    ClusterSpecAffinityTolerations(
                        key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                    )
                ],
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh", size="1Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            managed=ClusterSpecManaged(
                roles=[
                    ClusterSpecManagedRoles(
                        name="tfstate",
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name="tofu-state-db-credentials"),
                    )
                ]
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    """Write `db/` and the directory's `kustomization.yaml`; the Namespace beside it is
    `tofu_state.namespace`'s."""
    write_charts(root, _DB_DIR, chart)
    write_yaml(
        root / _DB_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[_CREDENTIALS_FILE, f"{_CLUSTER_NAME}.k8s.yaml"]),
    )
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=["namespace.k8s.yaml", "db"])
    )


def tofu_state_db(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        "tofu-state-db",
        artifact,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(cnpg),
    )
