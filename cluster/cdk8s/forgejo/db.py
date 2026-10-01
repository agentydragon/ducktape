"""Forgejo's metadata database: a CNPG `Cluster` on the SSD tier, owned by the `forgejo`
Kustomization, beside its hand-written application-user Secret."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/forgejo"
NAMESPACE = "forgejo"
DATABASE = "forgejo"
POSTGRES = cnpg.PostgresRef(
    name="forgejo-db-ssd",
    namespace=NAMESPACE,
    # Not `<cluster>-app`: CNPG reserves that name for a Secret it generates itself.
    app_secret=SecretRef(namespace=NAMESPACE, name="forgejo-db-ssd-creds"),
)


def _chart(app: App) -> Chart:
    chart = Chart(app, POSTGRES.name, disable_resource_name_hashes=True)
    cnpg.cluster(
        chart,
        "cluster",
        ref=POSTGRES,
        annotations={
            "description": (
                "Forgejo metadata DB on the SSD tier (physically cloned from the retired forgejo-db,"
                " Case B git-latency migration)"
            )
        },
        # Existing SSD-local replicas remain pinned by their PVs; the node
        # affinity only steers placements no existing claim constrains.
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-ssd",
        size="10Gi",
        # The cluster was created by pg_basebackup from the retired forgejo-db, so this
        # stanza never initializes anything. It names the application role CNPG
        # reconciles: the cloned owner `forgejo`, not CNPG's default `app`, which does
        # not exist here. CNPG keeps that role's password in sync with the Secret,
        # which Forgejo also authenticates with.
        initdb=cnpg.same_owner_initdb(DATABASE, secret=POSTGRES.app_secret),
        wal_archive=False,
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, _chart, manifest_name="db.k8s.yaml")
