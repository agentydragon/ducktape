"""The SeaweedFS replication PrometheusRule.

No ServiceMonitors here on purpose. The seaweedfs-operator generates one per component
(and per volumeTopology group) directly from the Seaweed CR's `metricsPort` fields, owning
them via ownerReferences -- so anything declared here is either ignored or fought over.
Verified 2026-08-06: all six live ServiceMonitors (master, volume, volume-hdd, volume-ssd,
filer, s3) carry ownerReferences to Seaweed/seaweedfs.

To change what is scraped, change `metricsPort` in `cluster.py`. Gotcha: the operator names
each metrics port `<component>-metrics` (e.g. `master-metrics`), NOT `metrics` -- a
hand-written ServiceMonitor referencing `metrics` matches no port and silently scrapes
nothing.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, group
from cluster.cdk8s.seaweedfs import namespace

NAME = "seaweedfs-monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/monitoring"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    PrometheusRule(
        chart,
        "seaweedfs-replication",
        metadata=metadata("seaweedfs-replication", namespace.NAME, labels={"release": "kube-prometheus-stack"}),
        groups=[
            group(
                "seaweedfs-replication",
                [
                    # SeaweedFS master does not self-heal under-replicated volumes. The operator's
                    # replication-repair AdminScript attempts hourly, copy-only repair; this alert
                    # still exposes stalled jobs, missing destination slots, and any excess or
                    # misplaced replicas, which the script deliberately does not delete.
                    #
                    # `SeaweedFS_master_replica_placement_mismatch` is a leader-only, per-volume
                    # gauge (labels: collection, id) = 1 when a volume's replica count/placement
                    # does not match its target (under- or over-replicated), else 0. Only the
                    # leader emits it, so `sum()` counts mismatched volumes cluster-wide.
                    PrometheusRuleSpecGroupsRules(
                        alert="SeaweedFSReplicaPlacementMismatch",
                        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
                            "sum(SeaweedFS_master_replica_placement_mismatch) > 0"
                        ),
                        for_="15m",
                        labels={"severity": "warning"},
                        annotations={
                            "summary": "SeaweedFS has {{ $value }} volume(s) off their replica count",
                            "description": (
                                "{{ $value }} SeaweedFS volume(s) have not matched their target replica "
                                "placement for 15m (replication 001 = 2 copies). The replication-repair "
                                "AdminScript attempts copy-only repair hourly; check its CronJob/Job status and "
                                "available volume slots if the mismatch persists. It never trims excess or "
                                "misplaced replicas, so review those deliberately. Suspend the AdminScript and "
                                "wait for any active Job before planned volume-server maintenance. For an "
                                "immediate manual repair, inspect `volume.list` and run "
                                "`volume.fix.replication -apply -doDelete=false` under `lock` from a master pod "
                                "(`weed shell` reads commands on stdin)."
                            ),
                        },
                    )
                ],
            )
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_monitoring(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_cluster: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        retry_interval=None,
        wait=None,
        suspend=False,
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_cluster,
            # PrometheusRule
            monitoring_crds,
        ),
    )
