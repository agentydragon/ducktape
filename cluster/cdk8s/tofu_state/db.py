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
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecPostgresql,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import CNPG_DATABASE_READY, manifest_file, write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/tofu-state"
_DB_DIR = f"{OUTPUT_DIR}/db"
_NAMESPACE = "tofu-state"
_CREDENTIALS_FILE = "credentials.sops.yaml"
DATABASE = cnpg.PostgresRef.generated(name="tofu-state-db-ovh", namespace=_NAMESPACE)


def chart(app: App) -> Chart:
    chart = Chart(app, DATABASE.name, disable_resource_name_hashes=True)
    cnpg.cluster(
        chart,
        "cluster",
        ref=DATABASE,
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
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-ssd",
        size="1Gi",
        # CNPG's default bootstrap: an `app` database and owner. Terraform's `tfstate` role is
        # managed below.
        initdb=None,
        wal_archive=False,
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
    )
    return chart


def write_manifests(root: Path) -> None:
    """Write `db/` and the directory's `kustomization.yaml`; the Namespace beside it is
    `tofu_state.namespace`'s."""
    write_yaml(
        root / _DB_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[_CREDENTIALS_FILE, write_charts(root, _DB_DIR, chart)]),
    )
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[manifest_file(OUTPUT_DIR), "db"])
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
