"""Agentplane staging's CNPG backups (`cluster/generated/agentplane-staging/db-backups`), which
the staging Cluster archives WAL to (`DbProps.wal_archive`)."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import cnpg_backup
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.staging import ENV
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agentplane-staging-db-backups"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/db-backups"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    cnpg_backup.CnpgBackup(chart, "backup", ref=database.postgres(ENV), policy=cnpg_backup.DAILY_30D)
    return chart


def agentplane_staging_db_backups(
    chart: Chart, directory: RenderedDirectory, cnpg: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_operator),
        description="Creates the agentplane staging CNPG backup schedule and its SeaweedFS storage.",
    )
