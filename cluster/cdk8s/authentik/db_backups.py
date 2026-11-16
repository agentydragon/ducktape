"""Authentik's CNPG backups (`cluster/generated/authentik/db-backups`), in their own Flux unit
beside the `authentik` Kustomization that applies the Cluster."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.authentik import db
from cluster.cdk8s.cnpg import backup
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "authentik-db-backups"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/db-backups"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    backup.CnpgBackup(chart, "backup", ref=db.POSTGRES, retention="30d", secret_name="authentik-db-backup-s3")
    return chart
