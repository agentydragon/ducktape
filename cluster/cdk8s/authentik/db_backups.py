"""Authentik's CNPG backups (`cluster/generated/authentik/db-backups`), in their own Flux unit
beside the `authentik` Kustomization that applies the Cluster."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import cnpg_backup
from cluster.cdk8s.authentik import db
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "authentik-db-backups"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/db-backups"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    cnpg_backup.CnpgBackup(chart, "backup", ref=db.POSTGRES, retention="30d", secret_name="authentik-db-backup-s3")
    return chart


def authentik_db_backups(
    chart: Chart, directory: RenderedDirectory, cnpg: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_operator),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )
