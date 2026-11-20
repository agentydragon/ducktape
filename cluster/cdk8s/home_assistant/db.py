"""The CNPG Cluster holding Home Assistant's recorder history and its barman-cloud backups.

One instance beside Home Assistant on the OptiPlex (Optiplex-single, cluster/docs/cnpg_conventions.md
R2), so recorder commits stay on-node. Base backups and WAL go to the `home-assistant-db-backups`
SeaweedFS bucket on OVH, which is what a lost OptiPlex disk recovers from.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s.cnpg import backup, cluster
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.home_assistant.namespace import NAMESPACE, OUTPUT_DIR

# Home Assistant's recorder will read `uri` from this Cluster's generated `-app` Secret.
DATABASE = cluster.PostgresRef.generated(name="home-assistant-db", namespace=NAMESPACE)


def chart(app: App) -> Chart:
    chart = Chart(app, DATABASE.name, disable_resource_name_hashes=True)
    cluster.cluster(
        chart,
        "cluster",
        ref=DATABASE,
        profile=cluster.OPTIPLEX_PIRAEUS_SSD,
        instances=1,
        size="10Gi",
        initdb=cluster.same_owner_initdb("homeassistant"),
        wal_archive=True,
    )
    backup.CnpgBackup(chart, "db-backup", ref=DATABASE, retention="30d")
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart, manifest_name="db.k8s.yaml")
