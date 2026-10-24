"""The CNPG Cluster holding Home Assistant's recorder history and its barman-cloud backups.

One instance beside Home Assistant on the OptiPlex (Optiplex-single, cluster/docs/cnpg_conventions.md
R2), so recorder commits stay on-node. Base backups and WAL go to the `home-assistant-db-backups`
SeaweedFS bucket on OVH, which is what a lost OptiPlex disk recovers from.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s import cnpg, cnpg_backup
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

_OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/home-assistant"
# Home Assistant's recorder will read `uri` from this Cluster's generated `-app` Secret.
DATABASE = cnpg.PostgresRef.generated(name="home-assistant-db", namespace="home-assistant")


def chart(app: App) -> Chart:
    chart = Chart(app, DATABASE.name, disable_resource_name_hashes=True)
    cnpg.cluster(
        chart,
        "cluster",
        ref=DATABASE,
        profile=cnpg.OPTIPLEX_PIRAEUS_SSD,
        instances=1,
        size="10Gi",
        initdb=cnpg.same_owner_initdb("homeassistant"),
        wal_archive=True,
    )
    cnpg_backup.CnpgBackup(chart, "db-backup", ref=DATABASE, retention="30d")
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart, manifest_name="db.k8s.yaml")
