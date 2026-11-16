"""Agentplane staging's CNPG backups (`cluster/generated/agentplane-staging/db-backups`), which
the staging Cluster archives WAL to (`database.DbProps.wal_archive`)."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.staging import ENV
from cluster.cdk8s.cnpg import backup
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agentplane-staging-db-backups"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/db-backups"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    # Staging writes ~17 GB/day of WAL, more than every other backed-up Cluster together; a week of
    # recovery points is enough history for a staging environment.
    backup.CnpgBackup(chart, "backup", ref=database.postgres(ENV), retention="7d")
    return chart
