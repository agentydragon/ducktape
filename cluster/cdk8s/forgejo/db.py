"""Forgejo's metadata database: a CNPG `Cluster` on the SSD tier, owned by the `forgejo`
Kustomization, beside its hand-written application-user Secret."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/forgejo/db"
NAMESPACE = "forgejo"
CLUSTER_NAME = "forgejo-db-ssd"
DATABASE = "forgejo"
# Not `<cluster>-app`: CNPG reserves that name for a Secret it generates itself.
CREDENTIALS_SECRET = f"{CLUSTER_NAME}-creds"
_CREDENTIALS_FILE = f"{CREDENTIALS_SECRET}.sops.yaml"
POSTGRES = cnpg.PostgresRef(
    name=CLUSTER_NAME, namespace=NAMESPACE, app_secret=SecretRef(namespace=NAMESPACE, name=CREDENTIALS_SECRET)
)


def _chart(app: App) -> Chart:
    chart = Chart(app, CLUSTER_NAME, disable_resource_name_hashes=True)
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
    write_charts(root, OUTPUT_DIR, _chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{CLUSTER_NAME}.k8s.yaml", _CREDENTIALS_FILE]),
    )
