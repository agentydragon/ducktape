"""PostgreSQL state backend for the GitOps OpenTofu roots.

Crossplane's OpenTofu provider manages the migrated roots. The remaining tofu-controller
runners use the same database. Each root keeps its state in its own PostgreSQL schema.

CNPG reconciles the tfstate role password from db/credentials.sops.yaml. Reflector copies
the Secret into flux-system for the providers' PGPASSWORD references. cluster/.envrc
reads the encrypted source directly.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecPostgresql,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.cnpg import backup, cluster
from cluster.cdk8s.cnpg.health import database_health
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.secret_ref import SecretRef

_NAMESPACE = "tofu-state"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{_NAMESPACE}"
CREDENTIALS_FILE = "db/credentials.sops.yaml"
DATABASE = cluster.PostgresRef.generated(name="tofu-state-db-ovh", namespace=_NAMESPACE)
STATE_DATABASE_NAME = "tfstate"
STATE_PASSWORD = SecretRef(namespace="flux-system", name="tofu-state-db-credentials").key("password")
STATE_CONNECTION_STRING = (
    f"postgres://tfstate@{DATABASE.rw.host}:{DATABASE.rw.port.number}/{STATE_DATABASE_NAME}?sslmode=disable"
)


def gitops_state_schema(name: str) -> str:
    """The default Postgres backend schema for `tf/gitops/<name>`."""
    return name.replace("-", "_")


def chart(app: App) -> Chart:
    chart = Chart(app, DATABASE.name, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO)
    cluster.cluster(
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
        profile=cluster.OVH_PIRAEUS_SSD,
        size="1Gi",
        resize_in_use_volumes=False,
        # CNPG's default bootstrap: an `app` database and owner. Terraform's `tfstate` role is
        # managed below.
        initdb=None,
        wal_archive=True,
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
    backup.CnpgBackup(chart, "db-backup", ref=DATABASE, retention="30d")
    return chart


def tofu_state_db(
    chart: Chart, directory: RenderedDirectory, cnpg: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "tofu-state-db",
        directory,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_check_exprs=[database_health()],
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_operator),
    )
